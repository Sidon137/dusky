#!/usr/bin/env python3
"""
==============================================================================
 UNIVERSAL DRIVE MANAGER — PLATINUM HYBRID EDITION (2026.09 / ARCH BLEEDING-EDGE)
 ------------------------------------------------------------------------------
 Target stack : Arch Linux, Linux 7.2+, Python 3.14+, util-linux 2.42+,
                cryptsetup 2.8+, systemd 258+, lsof 4.99+
 Runtime deps : python-keyring python-secretstorage python-rich
                util-linux cryptsetup lsof sudo systemd (systemd-run, udevadm)

 Design (2026 rewrite):
  - Kernel-native truth sources: /proc/self/mountinfo (mount table, maj:min match,
    btrfs anon-dev aware), /sys/class/block/*/holders + dm/uuid (crypt mapping
    discovery, ghost-mapper detection), /sys/.../queue/rotational (TRIM policy).
    -> zero JSON schema drift, zero child pollution, ~6 fewer subprocess spawns
       per drive than the findmnt/lsblk triple-query design.
  - sudo boundary: 'sudo -v' primed once on the main thread, kept alive by a
    daemon thread (sudo -n -v / 60 s), every privileged call is 'sudo -n ...'
    so a password prompt can never collide with a cryptsetup stdin pipe.
  - Interactive prompts (passphrase, busy-process resolver) run ONLY on the
    main thread. Workers return Outcome.NEEDS_PASSPHRASE and the main thread
    finishes them sequentially.
  - cryptsetup 2.8: open --type luks|bitlk --tries 1 --key-file - ;
    --allow-discards + --perf-no_{read,write}_workqueue only on non-rotational
    media; close -> 5x retry -> close --deferred -> forensics (lsof + sysfs holders).
  - mount 2.42: mount --mkdir -t <fs> -o <opts> --source UUID=... --target ...;
    NTFS uses the in-kernel 'ntfs' driver directly (no -i helper bypass needed,
    ntfs-3g/FUSE is not consulted).
  - TRIM: fstrim dispatched as a transient systemd unit (systemd-run --collect
    --no-block, idle IO class) -> no orphaned Popen, no zombies, journal visible.
  - Strict exit codes: 0 ok, 1 operational failure, 130 user cancel.
==============================================================================
"""

import argparse
import atexit
import enum
import fcntl
import getpass
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

try:
    import keyring
    import keyring.errors
    import secretstorage
    from rich.align import Align
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
except ImportError as exc:
    sys.stderr.write(
        f"[ERROR] Missing Python library: {exc.name}\n"
        "        Install once: sudo pacman -S --needed python-keyring python-secretstorage python-rich\n"
    )
    sys.exit(1)

# ------------------------------------------------------------------------------
#  CONSTANTS
# ------------------------------------------------------------------------------
VERSION: Final = "2026.09.0"
FILESYSTEM_TIMEOUT: Final = 15          # udev settle + by-uuid poll (seconds)
CRYPTSETUP_TIMEOUT: Final = 180         # argon2id on a throttled laptop can take >30 s
READ_PROBE_TIMEOUT: Final = 10
LOCK_MAX_RETRIES: Final = 5
LOCK_RETRY_DELAY: Final = 1.0
UMOUNT_MAX_ATTEMPTS: Final = 5
SIGTERM_GRACE: Final = 5.0
KEYRING_SERVICE: Final = "drive_manager"
KEYRING_GET_TIMEOUT: Final = 10
KEYRING_SET_TIMEOUT: Final = 60
SUDO_KEEPALIVE_INTERVAL: Final = 60
MAX_PARALLEL: Final = 8
ATTEMPT_HISTORY_CAP: Final = 50
ATTEMPT_HISTORY_SHOW: Final = 6

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1
EXIT_CANCEL: Final = 130

NON_POSIX_FSTYPES: Final = frozenset({"ntfs", "vfat", "exfat", "msdos"})
TRIM_FSTYPES: Final = frozenset({"ext4", "xfs", "f2fs", "vfat", "exfat", "ntfs", "btrfs"})
FSTYPE_ALIASES: Final = {"ntfs3": "ntfs", "fat32": "vfat", "fat": "vfat"}
PRUNE_ROOTS: Final = (Path("/mnt"), Path("/media"), Path("/run/media"), Path("/home"))
SYSFS_CPU: Final = Path("/sys/devices/system/cpu")

console = Console()
err_console = Console(stderr=True)
print_lock = threading.RLock()
_lock_fd: int | None = None


# ------------------------------------------------------------------------------
#  DATA STRUCTURES
# ------------------------------------------------------------------------------
class DriveType(enum.StrEnum):
    PROTECTED = "PROTECTED"
    SIMPLE = "SIMPLE"


class Outcome(enum.Enum):
    OK = enum.auto()
    FAILED = enum.auto()
    NEEDS_PASSPHRASE = enum.auto()   # worker could not finish without an interactive prompt


@dataclass(slots=True, frozen=True)
class Drive:
    name: str
    type: DriveType
    mountpoint: Path
    outer_uuid: str
    inner_uuid: str | None
    hint: str | None
    fstype: str | None
    mount_options: tuple[str, ...]
    symlinks: tuple[Path, ...]

    @property
    def fs_uuid(self) -> str:
        """UUID of the mountable filesystem (inner for PROTECTED, outer for SIMPLE)."""
        return self.inner_uuid if self.type is DriveType.PROTECTED and self.inner_uuid else self.outer_uuid

    @property
    def mapper_name(self) -> str:
        """Deterministic dm name, identical to the systemd/udisks convention."""
        return f"luks-{self.outer_uuid}"

    @property
    def mapper_path(self) -> Path:
        return Path("/dev/mapper") / self.mapper_name


class MountEntry(NamedTuple):
    mount_id: int
    majmin: str
    root: str
    target: Path
    fstype: str
    source: str
    options: str


class CryptMapping(NamedTuple):
    name: str
    dm_node: Path      # /dev/dm-N
    dm_uuid: str       # CRYPT-LUKS2-<hex>-<name> / CRYPT-BITLK-...


class Cmd(NamedTuple):
    rc: int
    out: str
    errtxt: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


# ------------------------------------------------------------------------------
#  LOGGING (THREAD-SAFE)
# ------------------------------------------------------------------------------
def log(msg: str) -> None:
    with print_lock:
        console.print(f"[bold blue]\\[DRIVE][/] {msg}")


def success(msg: str) -> None:
    with print_lock:
        console.print(f"[bold green]\\[SUCCESS][/] {msg}")


def warn(msg: str) -> None:
    with print_lock:
        console.print(f"[bold magenta]\\[WARN][/] {msg}")


def err(msg: str) -> None:
    with print_lock:
        err_console.print(f"[bold red]\\[ERROR][/] {msg}")


def hint_msg(msg: str) -> None:
    with print_lock:
        console.print(f"[bold yellow]\\[HINT][/] {msg}")


def cancel_exit() -> None:
    with print_lock:
        console.print()
        err("Cancelled by user.")
    sys.exit(EXIT_CANCEL)


# ------------------------------------------------------------------------------
#  SUBPROCESS PRIMITIVES
# ------------------------------------------------------------------------------
def run(argv: list[str], *, stdin: bytes | None = None, timeout: float | None = None) -> Cmd:
    """Run argv (shell=False), always capturing output. Never raises."""
    try:
        res = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Cmd(-1, "", f"timed out after {timeout}s: {' '.join(argv)}")
    except OSError as e:
        return Cmd(-1, "", f"{argv[0]}: {e.strerror}")
    return Cmd(res.returncode, res.stdout.decode(errors="replace"), res.stderr.decode(errors="replace"))


class Sudo:
    """sudo timestamp lifecycle. prime() is the ONLY interactive sudo call."""
    _stop = threading.Event()
    _started = False

    @staticmethod
    def prime() -> None:
        with print_lock:
            rc = subprocess.run(["sudo", "-v"]).returncode
        if rc != 0:
            err("sudo authentication failed. Cannot proceed.")
            sys.exit(EXIT_FAIL)

    @classmethod
    def ensure(cls) -> None:
        if run(["sudo", "-n", "-v"]).ok:
            return
        log("sudo timestamp expired; re-authenticating...")
        cls.prime()

    @classmethod
    def start_keepalive(cls) -> None:
        if cls._started:
            return
        cls._started = True

        def loop() -> None:
            while not cls._stop.wait(SUDO_KEEPALIVE_INTERVAL):
                run(["sudo", "-n", "-v"])

        threading.Thread(target=loop, daemon=True, name="sudo-keepalive").start()


def sudo(argv: list[str], *, stdin: bytes | None = None, timeout: float | None = None, report: bool = True) -> Cmd:
    """Privileged call. 'sudo -n' guarantees no password prompt can ever touch stdin."""
    res = run(["sudo", "-n", *argv], stdin=stdin, timeout=timeout)
    if res.rc == 1 and "password is required" in res.errtxt:
        Sudo.ensure()
        res = run(["sudo", "-n", *argv], stdin=stdin, timeout=timeout)
    if not res.ok and report and res.errtxt.strip():
        err(f"{escape(argv[0])} failed (rc={res.rc}): {escape(res.errtxt.strip())}")
    return res


# ------------------------------------------------------------------------------
#  SECURITY & ISOLATION
# ------------------------------------------------------------------------------
def prevent_root_execution() -> None:
    if os.geteuid() == 0:
        err("Do NOT run this script with sudo.")
        console.print("Running as root breaks access to your user's Secret Service keyring.")
        console.print("Privileges are requested internally, per command, via 'sudo -n'.")
        sys.exit(EXIT_FAIL)


def check_dependencies() -> None:
    import shutil
    deps = ["sudo", "mount", "umount", "lsblk", "udevadm", "cryptsetup", "lsof", "blockdev", "fstrim", "systemd-run"]
    missing = [d for d in deps if shutil.which(d) is None]
    if missing:
        err(f"Missing required commands: {', '.join(missing)}")
        hint_msg("sudo pacman -S --needed util-linux cryptsetup lsof systemd")
        sys.exit(EXIT_FAIL)


def get_runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/drive_manager (tmpfs, per-user, wiped at logout). Verified via O_NOFOLLOW|O_DIRECTORY fstat."""
    env = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not env:
        err("XDG_RUNTIME_DIR is not set. Run from a systemd-logind session (TTY, SSH or desktop).")
        sys.exit(EXIT_FAIL)
    path = Path(env) / "drive_manager"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        err(f"Security hazard: cannot open {path} safely ({e.strerror}). Symlink hijack?")
        sys.exit(EXIT_FAIL)
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        err(f"Security hazard: {path} is not owned by uid {os.getuid()} with mode 0700.")
        sys.exit(EXIT_FAIL)
    return path


def acquire_lock() -> None:
    """Exclusive kernel flock in the runtime dir; PID stamped for diagnostics."""
    global _lock_fd
    lock_path = get_runtime_dir() / "drive_manager.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        err(f"Could not open lock file {lock_path}: {e.strerror}")
        sys.exit(EXIT_FAIL)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = ""
        try:
            holder = os.read(fd, 32).decode(errors="replace").strip()
        except OSError:
            pass
        os.close(fd)
        err(f"Another drive_manager instance is running{f' (PID {holder})' if holder else ''}.")
        sys.exit(EXIT_FAIL)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd  # intentionally never closed: released by the kernel at exit


# ------------------------------------------------------------------------------
#  KERNEL TRUTH SOURCES: /proc/self/mountinfo, /sys/class/block, /dev/disk/by-uuid
# ------------------------------------------------------------------------------
_OCTAL_ESC = re.compile(r"\\([0-7]{3})")


def _unescape(s: str) -> str:
    return _OCTAL_ESC.sub(lambda m: chr(int(m.group(1), 8)), s)


def read_mountinfo() -> list[MountEntry]:
    entries: list[MountEntry] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        pre, sep, post = line.partition(" - ")
        if not sep:
            continue
        pf = pre.split(" ")
        po = post.split(" ")
        if len(pf) < 6 or len(po) < 2:
            continue
        entries.append(MountEntry(int(pf[0]), pf[2], _unescape(pf[3]), Path(_unescape(pf[4])), po[0], _unescape(po[1]), pf[5]))
    return entries


def device_majmin(dev: Path | None) -> str | None:
    if dev is None:
        return None
    try:
        st = os.stat(dev)
    except OSError:
        return None
    if not stat.S_ISBLK(st.st_mode):
        return None
    return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"


def mounts_for_device(dev: Path | None) -> list[Path]:
    """Every mountpoint backed by dev. Matches maj:min, and source path for btrfs (anonymous superblock dev)."""
    mm = device_majmin(dev)
    if not mm:
        return []
    hits: set[Path] = set()
    for e in read_mountinfo():
        if e.majmin == mm or (e.source.startswith("/dev/") and device_majmin(Path(e.source)) == mm):
            hits.add(e.target)
    return sorted(hits)


def mount_entry_for(target: Path) -> MountEntry | None:
    t = target.resolve()
    found: MountEntry | None = None
    for e in read_mountinfo():
        if e.target == t:
            found = e   # last entry wins == topmost mount
    return found


def resolve_device(uuid: str | None) -> Path | None:
    if not uuid:
        return None
    p = Path("/dev/disk/by-uuid") / uuid
    try:
        return p.resolve(strict=True)
    except OSError:
        return None


def sysfs_block(dev: Path) -> Path | None:
    node = Path("/sys/class/block") / dev.resolve().name
    return node if node.exists() else None


def is_rotational(dev: Path | None) -> bool:
    """queue/rotational of the device, its parent disk, or (dm) any of its slaves."""
    if dev is None:
        return False
    node = sysfs_block(dev)
    if node is None:
        return False
    real = node.resolve()
    for cand in (real / "queue" / "rotational", real.parent / "queue" / "rotational"):
        if cand.is_file():
            return cand.read_text().strip() == "1"
    slaves = real / "slaves"
    if slaves.is_dir():
        return any(is_rotational(Path("/dev") / s.name) for s in slaves.iterdir())
    return False


def probe_fstype(dev: Path | None) -> str | None:
    """libblkid superblock type as recorded by udev (lsblk -d: no children)."""
    if dev is None:
        return None
    res = run(["lsblk", "--json", "-d", "-o", "FSTYPE", str(dev)])
    if not res.ok:
        return None
    try:
        devices = json.loads(res.out).get("blockdevices", [])
    except json.JSONDecodeError:
        return None
    return (devices[0].get("fstype") or None) if devices else None


def _crypt_mapping_from_dm(dm_dir: Path) -> CryptMapping | None:
    name_f, uuid_f = dm_dir / "dm" / "name", dm_dir / "dm" / "uuid"
    if not name_f.is_file():
        return None
    uuid = uuid_f.read_text().strip() if uuid_f.is_file() else ""
    if not uuid.startswith("CRYPT-"):
        return None
    return CryptMapping(name_f.read_text().strip(), Path("/dev") / dm_dir.resolve().name, uuid)


def find_crypt_mapping(drive: Drive, outer_dev: Path | None) -> CryptMapping | None:
    """Live holder of the outer partition; falls back to a dm-uuid scan (covers ghost mappers after unplug)."""
    if outer_dev is not None:
        node = sysfs_block(outer_dev)
        holders = node / "holders" if node else None
        if holders and holders.is_dir():
            for h in sorted(holders.iterdir()):
                m = _crypt_mapping_from_dm(h)
                if m:
                    return m
    hexuuid = drive.outer_uuid.replace("-", "").lower()
    for dm_dir in sorted(Path("/sys/class/block").glob("dm-*")):
        m = _crypt_mapping_from_dm(dm_dir)
        if m and (m.name == drive.mapper_name or hexuuid in m.dm_uuid.lower()):
            return m
    return None


def mapping_is_live(mapping: CryptMapping) -> bool:
    """suspended==0, slaves still present, and one O_DIRECT 4 KiB read succeeds."""
    node = Path("/sys/class/block") / mapping.dm_node.name
    susp = node / "dm" / "suspended"
    if susp.is_file() and susp.read_text().strip() == "1":
        return False
    slaves = node / "slaves"
    if not slaves.is_dir() or not any(slaves.iterdir()):
        return False
    res = sudo(["dd", f"if={mapping.dm_node}", "of=/dev/null", "bs=4096", "count=1", "iflag=direct", "status=none"],
               timeout=READ_PROBE_TIMEOUT, report=False)
    if res.rc == -1:
        warn(f"Read probe on {mapping.dm_node} timed out after {READ_PROBE_TIMEOUT}s; treating mapping as unresponsive.")
    return res.ok


def wait_for_device(uuid: str, timeout: int = FILESYSTEM_TIMEOUT) -> Path | None:
    run(["udevadm", "settle", f"--timeout={timeout}"])
    deadline = time.monotonic() + timeout
    while True:
        dev = resolve_device(uuid)
        if dev or time.monotonic() >= deadline:
            return dev
        time.sleep(0.25)


# ------------------------------------------------------------------------------
#  KEYRING (SECRET SERVICE) — every D-Bus wait is bounded
# ------------------------------------------------------------------------------
def is_gui_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _secret_service_collection():
    """Default collection when keyring resolves to a Secret Service backend; None for other backends."""
    module = type(keyring.get_keyring()).__module__.lower()
    if not any(tag in module for tag in ("secretservice", "libsecret", "chainer")):
        return None
    conn = secretstorage.dbus_init()
    return secretstorage.get_default_collection(conn)


def _call_with_timeout(fn, timeout: float):
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True, name="keyring-io")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"Secret Service did not answer within {timeout}s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def ensure_keyring_unlocked() -> bool:
    try:
        coll = _call_with_timeout(_secret_service_collection, KEYRING_GET_TIMEOUT)
    except Exception as e:  # SecretServiceNotAvailableException, TimeoutError, ...
        log(f"Secret Service unavailable ({escape(type(e).__name__)}); using terminal prompt.")
        return False
    if coll is None or not coll.is_locked():
        return True
    if not is_gui_available():
        log("Keyring is locked and no DISPLAY/WAYLAND_DISPLAY is set (TTY/headless). Using terminal prompt.")
        return False
    log("Keyring is locked. Requesting unlock through the desktop Secret Service prompt...")
    try:
        dismissed = coll.unlock()
    except Exception as e:
        log(f"Keyring unlock prompt unavailable ({escape(str(e))}). Using terminal prompt.")
        return False
    if dismissed or coll.is_locked():
        log("Keyring unlock was dismissed.")
        return False
    success("Keyring unlocked.")
    return True


def keyring_get(name: str, timeout: float = KEYRING_GET_TIMEOUT) -> str | None:
    if not ensure_keyring_unlocked():
        return None
    try:
        value = _call_with_timeout(lambda: keyring.get_password(KEYRING_SERVICE, name), timeout)
    except TimeoutError:
        log("Keyring lookup timed out. Falling through to terminal prompt.")
        return None
    except Exception as e:
        log(f"Keyring lookup error ({escape(type(e).__name__)}). Falling through to terminal prompt.")
        return None
    return value if isinstance(value, str) and value else None


def keyring_set(name: str, secret: str, timeout: float = KEYRING_SET_TIMEOUT) -> bool:
    if not ensure_keyring_unlocked():
        err("Keyring locked or unavailable; passphrase NOT saved.")
        return False
    try:
        _call_with_timeout(lambda: keyring.set_password(KEYRING_SERVICE, name, secret), timeout)
    except TimeoutError:
        err(f"Keyring store timed out after {timeout}s; passphrase NOT saved.")
        return False
    except keyring.errors.KeyringLocked:
        err("Keyring is locked; passphrase NOT saved.")
        return False
    except Exception as e:
        err(f"Keyring store error: {escape(str(e))}")
        return False
    return True


# ------------------------------------------------------------------------------
#  FAILED-ATTEMPT HISTORY (tmpfs, 0600, atomic, wiped at logout)
# ------------------------------------------------------------------------------
def _attempts_path(name: str) -> Path:
    return get_runtime_dir() / f"attempts_{name}.json"


def load_attempts(name: str) -> list[str]:
    path = _attempts_path(name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or (st.st_mode & 0o077) or not stat.S_ISREG(st.st_mode):
            os.close(fd)
            path.unlink(missing_ok=True)
            return []
        with os.fdopen(fd, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []


def save_attempts(name: str, attempts: list[str]) -> None:
    path = _attempts_path(name)
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(attempts[-ATTEMPT_HISTORY_CAP:], f)
        os.replace(tmp, path)
    except OSError as e:
        warn(f"Could not persist attempt history: {e.strerror}")


def record_failed_attempt(name: str, secret: str) -> None:
    attempts = load_attempts(name)
    if secret not in attempts:
        attempts.append(secret)
        save_attempts(name, attempts)


def clear_attempts(name: str) -> None:
    _attempts_path(name).unlink(missing_ok=True)


def prompt_passphrase(drive: Drive) -> str:
    """MAIN THREAD ONLY. Shows hint + history panel, returns non-empty passphrase. Raises KeyboardInterrupt/EOFError."""
    while True:
        with print_lock:
            if drive.hint:
                hint_msg(f"Hint: {escape(drive.hint)}")
            attempts = load_attempts(drive.name)
            if attempts:
                shown = attempts[-ATTEMPT_HISTORY_SHOW:]
                hidden = len(attempts) - len(shown)
                lines: list[str] = []
                if hidden:
                    lines.append(f"[dim]... {hidden} older attempt{'s' if hidden > 1 else ''} hidden ...[/]")
                lines.extend(f"[red]✗[/] {escape(a)}" for a in shown)
                console.print(Align.right(Panel("\n".join(lines), title="[yellow]Previously Tried[/]",
                                                border_style="yellow", expand=False)))
            secret = Prompt.ask(
                f"Enter passphrase for [bold]{escape(drive.name)}[/] (/dev/disk/by-uuid/[bold cyan]{drive.outer_uuid}[/])",
                password=True,
            )
        secret = secret.rstrip("\r\n")
        if secret:
            return secret


# ------------------------------------------------------------------------------
#  CRYPT CONTAINER OPERATIONS
# ------------------------------------------------------------------------------
def container_type(outer_dev: Path) -> tuple[str | None, str | None]:
    probe = probe_fstype(outer_dev)
    match (probe or "").lower():
        case "crypto_luks":
            return "luks", probe
        case "bitlocker":
            return "bitlk", probe
        case _:
            return None, probe


def open_container(drive: Drive, outer_dev: Path, secret: str) -> bool:
    ctype, probe = container_type(outer_dev)
    if ctype is None:
        err(f"'{drive.name}': no LUKS/BitLocker superblock on {outer_dev} (blkid TYPE={probe or 'none'}).")
        hint_msg(f"Verify with: lsblk -d -o NAME,FSTYPE,UUID {outer_dev}")
        return False
    argv = ["cryptsetup", "open", "--type", ctype, "--tries", "1", "--key-file", "-"]
    if not is_rotational(outer_dev):
        argv.append("--allow-discards")
        if ctype == "luks":
            argv += ["--perf-no_read_workqueue", "--perf-no_write_workqueue"]
    argv += [str(outer_dev), drive.mapper_name]
    res = sudo(argv, stdin=secret.encode(), timeout=CRYPTSETUP_TIMEOUT, report=False)
    if res.ok:
        return True
    if res.rc == -1:
        err(f"cryptsetup timed out for '{drive.name}': {escape(res.errtxt)}")
    elif res.rc == 2:
        err(f"Decryption failed for '{drive.name}': passphrase rejected.")
    else:
        err(f"cryptsetup open failed for '{drive.name}' (rc={res.rc}): {escape(res.errtxt.strip())}")
    return False


def close_container(name: str, *, deferred: bool = False, report: bool = False) -> bool:
    argv = ["cryptsetup", "close"] + (["--deferred"] if deferred else []) + [name]
    return sudo(argv, report=report).ok


def crypt_forensics(mapping: CryptMapping) -> None:
    log(f"Forensics for /dev/mapper/{mapping.name} ({mapping.dm_node})...")
    res = sudo(["lsof", "-w", str(mapping.dm_node), f"/dev/mapper/{mapping.name}"], report=False)
    if res.out.strip():
        with print_lock:
            console.print(Panel(escape(res.out.strip()), title="Processes holding the crypt node", border_style="red"))
    holders = Path("/sys/class/block") / mapping.dm_node.name / "holders"
    kernel_holders = sorted(h.name for h in holders.iterdir()) if holders.is_dir() else []
    if kernel_holders:
        hint_msg(f"Kernel block holders (LVM/RAID/another dm layer): {', '.join(kernel_holders)}")
    status = sudo(["cryptsetup", "status", mapping.name], report=False)
    if status.out.strip():
        with print_lock:
            console.print(Panel(escape(status.out.strip()), title="cryptsetup status", border_style="yellow"))
    hint_msg("Next: 'sudo dmesg | tail -n 30' ; 'findmnt --json -v' ; "
             f"'sudo cryptsetup close --deferred {mapping.name}' once the holder is gone.")


# ------------------------------------------------------------------------------
#  UNMOUNT / BUSY-PROCESS RESOLVER (MAIN THREAD FOR PROMPTS)
# ------------------------------------------------------------------------------
def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse_lsof(out: str) -> list[dict[str, str]]:
    procs: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            cur = {"pid": val, "cmd": "unknown", "user": "unknown"}
            procs.append(cur)
        elif cur is not None and tag == "c":
            cur["cmd"] = val
        elif cur is not None and tag == "u":
            cur["user"] = val
    seen: set[str] = set()
    unique = []
    for p in procs:
        if p["pid"] not in seen and p["pid"].isdigit():
            seen.add(p["pid"])
            unique.append(p)
    return unique


def resolve_busy_processes(mountpoint: Path, *, interactive: bool) -> bool:
    """lsof via sudo (defeats hidepid=2). Returns True when at least one holder was terminated."""
    res = sudo(["lsof", "-w", "-F", "pcu", "+f", "--", str(mountpoint)], report=False)
    procs = parse_lsof(res.out)
    if not procs:
        return False
    with print_lock:
        console.print(Panel(
            "[bold red]FILESYSTEM IS BUSY[/]\n\n"
            f"The following processes hold files under [bold white]{escape(str(mountpoint))}[/].",
            title="Filesystem Locked", border_style="red"))
        table = Table(show_header=True, header_style="bold yellow", border_style="yellow")
        table.add_column("COMMAND", style="cyan")
        table.add_column("PID", justify="right", style="yellow")
        table.add_column("USER")
        for p in procs:
            table.add_row(escape(p["cmd"]), p["pid"], escape(p["user"]))
        console.print(table)
    if not interactive:
        hint_msg("Parallel mode: busy-process resolution is deferred to the main thread.")
        return False
    acted = False
    for p in procs:
        pid = int(p["pid"])
        if not process_alive(pid):
            continue
        with print_lock:
            ans = Prompt.ask(f"Terminate [bold cyan]{escape(p['cmd'])}[/] (PID [bold yellow]{pid}[/])?",
                             choices=["y", "n"], default="y")
        if ans != "y":
            continue
        if not sudo(["kill", "-TERM", str(pid)], report=True).ok:
            continue
        deadline = time.monotonic() + SIGTERM_GRACE
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.25)
        if process_alive(pid):
            warn(f"PID {pid} ignored SIGTERM; sending SIGKILL.")
            sudo(["kill", "-KILL", str(pid)], report=True)
            time.sleep(0.25)
        if not process_alive(pid):
            success(f"Terminated {escape(p['cmd'])} (PID {pid}).")
            acted = True
    return acted


def unmount_path(mountpoint: Path, *, interactive: bool) -> bool:
    if mount_entry_for(mountpoint) is None:
        return True
    log(f"Unmounting {escape(str(mountpoint))}...")
    for attempt in range(1, UMOUNT_MAX_ATTEMPTS + 1):
        res = sudo(["umount", str(mountpoint)], report=False)
        if res.ok or mount_entry_for(mountpoint) is None:
            log(f"Unmounted {escape(str(mountpoint))}.")
            return True
        busy = "busy" in res.errtxt.lower()
        if busy:
            log(f"{escape(str(mountpoint))} is busy (attempt {attempt}/{UMOUNT_MAX_ATTEMPTS}); scanning holders...")
            if not resolve_busy_processes(mountpoint, interactive=interactive):
                if not interactive:
                    break
                log("No userspace holder terminated; waiting for the kernel to settle...")
        else:
            err(f"umount {escape(str(mountpoint))}: {escape(res.errtxt.strip())}")
            break
        time.sleep(1)
    err(f"Failed to unmount {escape(str(mountpoint))}.")
    hint_msg(f"Inspect with: sudo lsof +f -- {mountpoint} ; findmnt --json -v --mountpoint {mountpoint}")
    return False


def prune_stale_dir(path: Path, keep: set[Path]) -> None:
    """rmdir the (now empty) stale mountpoint, then climb while parents are empty and inside a prune root.
    Never touches: $HOME itself, anything above the prune roots, configured mountpoints, non-empty or mounted dirs."""
    home = Path.home()
    p = path.resolve()
    first = True
    while True:
        if p in keep or p == home or p == Path("/") or p in PRUNE_ROOTS:
            return
        if not any(p.is_relative_to(r) for r in PRUNE_ROOTS):
            return
        if not first and p.is_relative_to(home):
            return  # never climb inside the active user's home
        try:
            if p.is_symlink() or not p.is_dir() or mount_entry_for(p) or any(p.iterdir()):
                return
        except OSError:
            return
        if not sudo(["rmdir", str(p)], report=False).ok:
            return
        log(f"Pruned empty stale directory {escape(str(p))}.")
        first = False
        p = p.parent


# ------------------------------------------------------------------------------
#  INTEGRATIONS: ownership + declarative symlinks (rename-not-delete)
# ------------------------------------------------------------------------------
def reconcile_integrations(drive: Drive) -> None:
    uid, gid = os.getuid(), os.getgid()
    home = Path.home()
    target = drive.mountpoint.resolve()
    if target.is_relative_to(home):
        try:
            st = target.stat()
            if st.st_uid != uid or st.st_gid != gid:
                log(f"Adjusting ownership of {escape(str(target))} to {uid}:{gid}...")
                sudo(["chown", f"{uid}:{gid}", str(target)])
        except OSError as e:
            warn(f"Could not stat {escape(str(target))}: {e.strerror}")
    for link in drive.symlinks:
        try:
            if link.is_symlink():
                current = (link.parent / os.readlink(link)).resolve()
                if current == target:
                    continue
                log(f"Re-pointing symlink {escape(str(link))} -> {escape(str(target))}")
                link.unlink()
            elif link.exists():
                backup = link.with_name(f"{link.name}.pre-drive-manager.{time.strftime('%Y%m%d-%H%M%S')}")
                warn(f"{escape(str(link))} is a real path; moving it to {escape(str(backup))} (nothing is deleted).")
                link.rename(backup)
            else:
                link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            os.lchown(link, uid, gid)
            success(f"Symlink ready: {escape(str(link))} -> {escape(str(target))}")
        except OSError as e:
            err(f"Symlink reconcile failed for {escape(str(link))}: {e.strerror}")
            hint_msg(f"Inspect with: ls -ld {link} ; findmnt --mountpoint {link}")


# ------------------------------------------------------------------------------
#  MOUNT POLICY
# ------------------------------------------------------------------------------
def normalize_fstype(fs: str | None) -> str | None:
    if not fs:
        return None
    fs = fs.lower()
    return FSTYPE_ALIASES.get(fs, fs)


def audit_mount_options(drive: Drive, fstype: str | None, rotational: bool) -> None:
    opts = set(drive.mount_options)
    if "force" in opts:
        warn(f"'{drive.name}': 'force' is set — this clears the Windows dirty bit and risks NTFS corruption.")
    if "discard" in opts and rotational:
        warn(f"'{drive.name}': 'discard' on a rotational disk is a no-op; remove it.")
    if "discard" in opts and fstype == "ext4":
        hint_msg(f"'{drive.name}': ext4 synchronous 'discard' stalls unlink(); prefer the background fstrim this tool dispatches.")
    if "autodefrag" in opts and not rotational:
        warn(f"'{drive.name}': 'autodefrag' on an SSD multiplies write amplification; remove it.")
    if fstype == "btrfs" and not rotational and not any(o.startswith("discard") for o in opts):
        hint_msg(f"'{drive.name}': btrfs on SSD — add 'discard=async' for native non-blocking TRIM.")
    if "flush" in opts:
        warn(f"'{drive.name}': 'flush' destroys flash endurance; remove it.")


def build_mount_argv(drive: Drive, source: str, fstype: str | None) -> list[str]:
    uid, gid = os.getuid(), os.getgid()
    argv = ["mount", "--mkdir"]
    if fstype:
        argv += ["-t", fstype]
    options: list[str] = []
    if drive.mount_options:
        for opt in drive.mount_options:
            if opt.startswith("uid="):
                options.append(f"uid={uid}")
            elif opt.startswith("gid="):
                options.append(f"gid={gid}")
            else:
                options.append(opt)
    elif fstype in NON_POSIX_FSTYPES:
        options.append(f"uid={uid},gid={gid},dmask=022,fmask=133")
        log(f"Auto-configured ownership for non-POSIX filesystem {fstype.upper()}.")
    if options:
        argv += ["-o", ",".join(options)]
    argv += ["--source", source, "--target", str(drive.mountpoint)]
    return argv


def dispatch_trim(drive: Drive, dev: Path | None, fstype: str | None, rotational: bool) -> None:
    if rotational or fstype not in TRIM_FSTYPES:
        return
    if any(o.startswith("discard") for o in drive.mount_options):
        return  # discard / discard=async already handles it at the FS layer
    unit = f"drive-manager-trim-{re.sub(r'[^a-zA-Z0-9_.-]', '_', drive.name)}-{int(time.time())}"
    res = sudo(["systemd-run", "--quiet", "--collect", "--no-block", f"--unit={unit}",
                "-p", "Nice=19", "-p", "IOSchedulingClass=idle",
                "fstrim", "--quiet-unsupported", str(drive.mountpoint)], report=False)
    if res.ok:
        log(f"Background TRIM dispatched for '{drive.name}' (journalctl -u {unit}).")
    else:
        warn(f"Could not dispatch TRIM for '{drive.name}': {escape(res.errtxt.strip())}")


# ------------------------------------------------------------------------------
#  CORE ENGINE
# ------------------------------------------------------------------------------
def show_status(drives: dict[str, Drive]) -> None:
    table = Table(show_header=True, header_style="bold white", border_style="bright_black")
    table.add_column("DRIVE", width=14)
    table.add_column("TYPE", width=10)
    table.add_column("FS", width=8)
    table.add_column("CRYPT", width=8)
    table.add_column("STATUS", width=11)
    table.add_column("MOUNTPOINT")
    for name, drive in sorted(drives.items()):
        fs_dev = resolve_device(drive.fs_uuid)
        mounts = mounts_for_device(fs_dev)
        fstype = probe_fstype(fs_dev) or drive.fstype or "?"
        crypt = "—"
        if drive.type is DriveType.PROTECTED:
            outer = resolve_device(drive.outer_uuid)
            if outer is None:
                crypt = "[dim]absent[/]"
            else:
                crypt = "[green]open[/]" if find_crypt_mapping(drive, outer) else "[red]closed[/]"
        mp = drive.mountpoint
        if mp in mounts:
            table.add_row(f"[bold green]●[/] {name}", drive.type, fstype, crypt, "[bold green]Mounted[/]", str(mp))
        elif mounts:
            table.add_row(f"[bold yellow]▲[/] {name}", drive.type, fstype, crypt, "[bold yellow]Divergent[/]",
                          f"{', '.join(map(str, mounts))} (expected {mp})")
        else:
            table.add_row(f"[bold red]○[/] {name}", drive.type, fstype, crypt, "[bold red]Unmounted[/]", str(mp))
    with print_lock:
        console.print()
        console.print(table)
        console.print()


def do_unlock(drive: Drive, secret: str | None, *, interactive: bool, all_mountpoints: set[Path]) -> Outcome:
    """interactive=True only on the main thread. Workers get interactive=False and may return NEEDS_PASSPHRASE."""
    log(f"Unlock sequence for '{drive.name}' started.")
    target = drive.mountpoint

    # --- Step 1: mount-table reconciliation -------------------------------------------------
    fs_dev = resolve_device(drive.fs_uuid)
    mounts = mounts_for_device(fs_dev)
    if target in mounts:
        for stale in (m for m in mounts if m != target):
            log(f"Removing redundant stale mount at {escape(str(stale))}...")
            if unmount_path(stale, interactive=interactive):
                prune_stale_dir(stale, all_mountpoints)
        reconcile_integrations(drive)
        success(f"'{drive.name}' is already mounted at {escape(str(target))}.")
        return Outcome.OK
    for stale in mounts:
        log(f"'{drive.name}' is mounted at divergent path {escape(str(stale))}; relocating to {escape(str(target))}...")
        if not unmount_path(stale, interactive=interactive):
            err(f"Cannot relocate '{drive.name}': divergent mount {escape(str(stale))} is still busy.")
            return Outcome.FAILED
        prune_stale_dir(stale, all_mountpoints)

    # --- Step 2: crypt container --------------------------------------------------------------
    mapping: CryptMapping | None = None
    if drive.type is DriveType.PROTECTED:
        outer_dev = resolve_device(drive.outer_uuid)
        if outer_dev is None:
            err(f"Physical device for '{drive.name}' not present (outer UUID {drive.outer_uuid}).")
            hint_msg("Is it plugged in? Check: lsblk -o NAME,FSTYPE,UUID ; udevadm settle")
            return Outcome.FAILED
        mapping = find_crypt_mapping(drive, outer_dev)
        if mapping and mapping_is_live(mapping):
            log(f"Crypt container for '{drive.name}' is already open as /dev/mapper/{mapping.name}.")
        else:
            if mapping:
                warn(f"Stale/unresponsive mapping /dev/mapper/{mapping.name}; closing before re-open...")
                if not close_container(mapping.name, report=True):
                    err(f"Cannot close stale mapping {mapping.name}.")
                    crypt_forensics(mapping)
                    return Outcome.FAILED
                run(["udevadm", "settle", "--timeout=5"])
                mapping = None
            prompted_here = False
            while True:
                if secret is None:
                    if not interactive:
                        return Outcome.NEEDS_PASSPHRASE
                    secret = prompt_passphrase(drive)
                    prompted_here = True
                log(f"Opening container for '{drive.name}'...")
                if open_container(drive, outer_dev, secret):
                    clear_attempts(drive.name)
                    break
                record_failed_attempt(drive.name, secret)
                secret = None
                if not interactive:
                    return Outcome.NEEDS_PASSPHRASE
            if prompted_here and secret is not None and keyring_set(drive.name, secret):
                success(f"Passphrase for '{drive.name}' saved to the keyring.")
            secret = None
            fs_dev = wait_for_device(drive.inner_uuid or "")
            mapping = find_crypt_mapping(drive, outer_dev)
            if fs_dev is None:
                if mapping is None:
                    err(f"Container opened but no mapping found for '{drive.name}' — udev/dm inconsistency.")
                    hint_msg(f"Check: lsblk --tree {outer_dev} ; sudo dmsetup ls --target crypt")
                    return Outcome.FAILED
                hint_msg(f"udev did not publish /dev/disk/by-uuid/{drive.inner_uuid}; mounting via /dev/mapper/{mapping.name}.")
                if probe_fstype(mapping.dm_node) is None:
                    err(f"Container opened but no filesystem detected on /dev/mapper/{mapping.name}.")
                    hint_msg(f"Check inner_uuid in drives.toml: lsblk -f /dev/mapper/{mapping.name}")
                    return Outcome.FAILED

    # --- Step 3: mount --------------------------------------------------------------------
    if fs_dev is not None:
        source, source_dev = f"UUID={drive.fs_uuid}", fs_dev
    elif mapping is not None:
        source, source_dev = f"/dev/mapper/{mapping.name}", mapping.dm_node
    else:
        err(f"Filesystem UUID {drive.fs_uuid} for '{drive.name}' not present.")
        hint_msg("Check: ls -l /dev/disk/by-uuid/ ; lsblk -f")
        return Outcome.FAILED
    fstype = normalize_fstype(drive.fstype) or normalize_fstype(probe_fstype(source_dev))
    rotational = is_rotational(source_dev)
    audit_mount_options(drive, fstype, rotational)
    argv = build_mount_argv(drive, source, fstype)
    log(f"Mounting '{drive.name}' ({fstype or 'auto'}, {'HDD' if rotational else 'SSD/NVMe'}) at {escape(str(target))}...")
    res = sudo(argv, report=False)
    if not res.ok:
        err(f"mount failed for '{drive.name}' (rc={res.rc}): {escape(res.errtxt.strip())}")
        hint_msg(f"Next: sudo dmesg | tail -n 20 ; lsblk -f {source_dev} ; check mount_options for {fstype or 'this fs'}")
        return Outcome.FAILED
    success(f"'{drive.name}' mounted at {escape(str(target))}.")
    reconcile_integrations(drive)
    dispatch_trim(drive, source_dev, fstype, rotational)
    return Outcome.OK


def do_lock(drive: Drive, *, interactive: bool, all_mountpoints: set[Path]) -> Outcome:
    log(f"Lock sequence for '{drive.name}' started.")

    # --- Step 1: unmount everywhere ----------------------------------------------------------
    fs_dev = resolve_device(drive.fs_uuid)
    mounts = set(mounts_for_device(fs_dev))
    if mount_entry_for(drive.mountpoint):
        mounts.add(drive.mountpoint.resolve())
    if mounts:
        for mp in sorted(mounts, reverse=True):   # deepest first
            if not unmount_path(mp, interactive=interactive):
                err(f"Aborting lock of '{drive.name}': {escape(str(mp))} could not be unmounted.")
                return Outcome.FAILED
            prune_stale_dir(mp, all_mountpoints)
        log(f"All mountpoints of '{drive.name}' unmounted.")
    else:
        log(f"'{drive.name}' is not mounted.")

    if drive.type is DriveType.SIMPLE:
        success(f"'{drive.name}' released.")
        return Outcome.OK

    # --- Step 2: close crypt container -------------------------------------------------------
    outer_dev = resolve_device(drive.outer_uuid)
    mapping = find_crypt_mapping(drive, outer_dev)
    if mapping is None:
        if outer_dev is None:
            success(f"'{drive.name}' is physically absent and no mapping remains.")
        else:
            success(f"Container of '{drive.name}' is already locked.")
        return Outcome.OK
    if outer_dev is None:
        warn(f"Physical device gone but ghost mapping /dev/mapper/{mapping.name} remains; forcing teardown.")

    sudo(["blockdev", "--flushbufs", str(mapping.dm_node)], report=False)
    log(f"Closing crypt node {mapping.name}...")
    if close_container(mapping.name):
        success(f"'{drive.name}' locked.")
        return Outcome.OK
    for attempt in range(1, LOCK_MAX_RETRIES + 1):
        time.sleep(LOCK_RETRY_DELAY)
        if close_container(mapping.name):
            success(f"'{drive.name}' locked (attempt {attempt + 1}).")
            return Outcome.OK
        log(f"Close attempt {attempt}/{LOCK_MAX_RETRIES} for '{drive.name}' failed; retrying...")
    log(f"'{drive.name}' is still held; requesting deferred close (kernel removes it when the last opener exits)...")
    if close_container(mapping.name, deferred=True):
        success(f"'{drive.name}' marked for deferred closure. Verify later with: sudo cryptsetup status {mapping.name}")
        return Outcome.OK
    err(f"All close strategies failed for {mapping.name}.")
    crypt_forensics(mapping)
    return Outcome.FAILED


# ------------------------------------------------------------------------------
#  PIPELINES (prompts only on the main thread)
# ------------------------------------------------------------------------------
def _container_needs_secret(drive: Drive) -> bool:
    if drive.type is not DriveType.PROTECTED:
        return False
    if drive.mountpoint in mounts_for_device(resolve_device(drive.fs_uuid)):
        return False
    outer = resolve_device(drive.outer_uuid)
    if outer is None:
        return False   # do_unlock will report the missing device
    mapping = find_crypt_mapping(drive, outer)
    return not (mapping and mapping_is_live(mapping))


def unlock_pipeline(drives: dict[str, Drive], targets: list[str]) -> int:
    all_mps = {d.mountpoint for d in drives.values()}
    secrets: dict[str, str | None] = {}
    prompted: set[str] = set()

    # Phase 1 — sequential credential collection (main thread)
    for name in targets:
        drive = drives[name]
        secrets[name] = None
        if not _container_needs_secret(drive):
            continue
        secret = keyring_get(drive.name)
        if secret is None:
            log(f"No keyring entry for '[bold cyan]{escape(drive.name)}[/]'.")
            secret = prompt_passphrase(drive)
            prompted.add(name)
        secrets[name] = secret

    # Phase 2 — parallel unlock+mount (no prompts inside workers)
    outcomes: dict[str, Outcome] = {}
    if len(targets) == 1:
        name = targets[0]
        outcomes[name] = do_unlock(drives[name], secrets[name], interactive=True, all_mountpoints=all_mps)
    else:
        log(f"Dispatching parallel unlock for {len(targets)} drives...")
        with ThreadPoolExecutor(max_workers=min(len(targets), MAX_PARALLEL), thread_name_prefix="unlock") as pool:
            futs = {pool.submit(do_unlock, drives[n], secrets[n], interactive=False, all_mountpoints=all_mps): n for n in targets}
            for fut in as_completed(futs):
                n = futs[fut]
                try:
                    outcomes[n] = fut.result()
                except Exception as e:  # noqa: BLE001 - worker bug must not take down siblings
                    err(f"Internal error unlocking '{n}': {escape(repr(e))}")
                    outcomes[n] = Outcome.FAILED
        # Phase 3 — finish anything that needs a human, sequentially
        for n in targets:
            if outcomes.get(n) is Outcome.NEEDS_PASSPHRASE:
                log(f"'{n}' needs interactive input; continuing on the main thread...")
                outcomes[n] = do_unlock(drives[n], None, interactive=True, all_mountpoints=all_mps)

    # Persist passphrases that were typed in Phase 1 and proven correct
    for n in prompted:
        if outcomes.get(n) is Outcome.OK and secrets.get(n) and keyring_set(n, secrets[n] or ""):
            success(f"Passphrase for '{n}' saved to the keyring.")
    secrets.clear()

    failed = [n for n, o in outcomes.items() if o is not Outcome.OK]
    if failed:
        err(f"Unlock incomplete for: {', '.join(failed)}")
        return EXIT_FAIL
    return EXIT_OK


def lock_pipeline(drives: dict[str, Drive], targets: list[str]) -> int:
    all_mps = {d.mountpoint for d in drives.values()}
    outcomes: dict[str, Outcome] = {}
    if len(targets) == 1:
        outcomes[targets[0]] = do_lock(drives[targets[0]], interactive=True, all_mountpoints=all_mps)
    else:
        log(f"Dispatching parallel lock for {len(targets)} drives...")
        with ThreadPoolExecutor(max_workers=min(len(targets), MAX_PARALLEL), thread_name_prefix="lock") as pool:
            futs = {pool.submit(do_lock, drives[n], interactive=False, all_mountpoints=all_mps): n for n in targets}
            for fut in as_completed(futs):
                n = futs[fut]
                try:
                    outcomes[n] = fut.result()
                except Exception as e:  # noqa: BLE001
                    err(f"Internal error locking '{n}': {escape(repr(e))}")
                    outcomes[n] = Outcome.FAILED
        for n in targets:
            if outcomes.get(n) is Outcome.FAILED and mounts_for_device(resolve_device(drives[n].fs_uuid)):
                log(f"'{n}' still mounted after parallel pass; retrying interactively on the main thread...")
                outcomes[n] = do_lock(drives[n], interactive=True, all_mountpoints=all_mps)
    failed = [n for n, o in outcomes.items() if o is not Outcome.OK]
    if failed:
        err(f"Lock incomplete for: {', '.join(failed)}")
        return EXIT_FAIL
    return EXIT_OK


def set_password(drives: dict[str, Drive], name: str) -> bool:
    drive = drives.get(name)
    if drive is None:
        err(f"Drive '{name}' is not in the configuration.")
        return False
    if drive.type is not DriveType.PROTECTED:
        err(f"'{name}' is a SIMPLE drive and has no passphrase.")
        return False
    console.print(Panel(f"Storing keyring passphrase for [bold cyan]{escape(name)}[/] "
                        f"(service '{KEYRING_SERVICE}', account '{escape(name)}').",
                        title="Keyring Setup", border_style="cyan"))
    try:
        pwd = getpass.getpass(f"Passphrase for '{name}': ")
        confirm = getpass.getpass("Confirm: ")
    except (KeyboardInterrupt, EOFError):
        cancel_exit()
        return False
    if not pwd:
        err("Empty passphrase refused.")
        return False
    if pwd != confirm:
        err("Passphrases do not match.")
        return False
    if keyring_set(name, pwd):
        clear_attempts(name)
        success(f"Passphrase stored for '{name}'.")
        return True
    return False


# ------------------------------------------------------------------------------
#  CPU ACCELERATOR (hybrid topologies with user-offlined P-cores)
# ------------------------------------------------------------------------------
def parse_cpulist(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.strip().split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        cpus.extend(range(int(lo), int(hi or lo) + 1))
    return cpus


class CPUAccelerator:
    """Temporarily onlines offline performance cores; restores on exit, exception, SIGTERM/SIGHUP (not SIGKILL)."""

    def __init__(self) -> None:
        self.enabled: list[int] = []
        atexit.register(self.restore)

    @staticmethod
    def performance_cores() -> list[int]:
        override = os.environ.get("DRIVE_MANAGER_PCORES", "").strip()
        if override:
            return parse_cpulist(override)
        intel = Path("/sys/devices/cpu_core/cpus")          # Intel hybrid PMU cpumask
        if intel.is_file():
            return parse_cpulist(intel.read_text())
        perf: dict[int, int] = {}                             # AMD/other: ACPI CPPC highest_perf spread
        for node in SYSFS_CPU.glob("cpu[0-9]*"):
            f = node / "acpi_cppc" / "highest_perf"
            if f.is_file():
                txt = f.read_text().strip()
                if txt.isdigit():
                    perf[int(node.name[3:])] = int(txt)
        if len(set(perf.values())) < 2:
            return []
        lo, hi = min(perf.values()), max(perf.values())
        if (hi - lo) / hi <= 0.15:
            return []
        mid = (lo + hi) / 2
        return sorted(c for c, v in perf.items() if v >= mid)

    def __enter__(self) -> "CPUAccelerator":
        offline = []
        for cpu in self.performance_cores():
            f = SYSFS_CPU / f"cpu{cpu}" / "online"
            try:
                if f.is_file() and f.read_text().strip() == "0":
                    offline.append(cpu)
            except OSError:
                pass
        if offline:
            log(f"Offline performance cores {offline}: enabling for the duration of this run...")
            for cpu in offline:
                if sudo(["tee", str(SYSFS_CPU / f"cpu{cpu}" / "online")], stdin=b"1", report=False).ok:
                    self.enabled.append(cpu)
                else:
                    warn(f"Could not online cpu{cpu} (CONFIG_HOTPLUG_CPU disabled or cpu locked).")
        return self

    def __exit__(self, *exc) -> None:
        self.restore()

    def restore(self) -> None:
        if not self.enabled:
            return
        log("Restoring CPU power-saving state (offlining performance cores)...")
        for cpu in list(self.enabled):
            path = SYSFS_CPU / f"cpu{cpu}" / "online"
            for _ in range(5):
                if sudo(["tee", str(path)], stdin=b"0", report=False).ok:
                    break
                time.sleep(0.05)
        self.enabled.clear()


# ------------------------------------------------------------------------------
#  CONFIG: strict schema validation + path normalization
# ------------------------------------------------------------------------------
UUID_PATTERNS: Final = (
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),  # ext4/btrfs/xfs/LUKS (lowercase)
    re.compile(r"^[0-9A-F]{16}$"),                                                    # NTFS (uppercase)
    re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}$"),                                        # vfat/exFAT (uppercase)
)
ALLOWED_KEYS: Final = frozenset({"type", "mountpoint", "outer_uuid", "inner_uuid", "hint", "fstype", "mount_options", "symlinks"})
NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigError(ValueError):
    pass


def resolve_configured_path(raw: str, *, resolve_symlinks: bool) -> Path:
    """Expands $VARS and ~, rebases legacy /home/<olduser>/... onto the active $HOME."""
    expanded = os.path.expandvars(raw.strip())
    p = Path(expanded).expanduser()
    if not p.is_absolute():
        raise ConfigError(f"path must be absolute after expansion: '{raw}'")
    home = Path.home()
    parts = p.parts
    if len(parts) > 2 and parts[1] == "home" and parts[2] != home.name:
        p = home.joinpath(*parts[3:])
    return p.resolve() if resolve_symlinks else p.absolute()


def _validate_uuid(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not any(rx.match(value) for rx in UUID_PATTERNS):
        raise ConfigError(f"{field_name}='{value}' is not a valid blkid UUID "
                          "(lowercase 8-4-4-4-12, uppercase 16-hex NTFS, or uppercase XXXX-XXXX FAT). "
                          "Case matters: /dev/disk/by-uuid is case-sensitive.")
    return value


def parse_drive(name: str, data: object) -> Drive:
    if not NAME_RE.match(name):
        raise ConfigError("drive name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    if not isinstance(data, dict):
        raise ConfigError("drive entry must be a table")
    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(sorted(unknown))}")
    for key in ("type", "mountpoint", "outer_uuid"):
        if key not in data:
            raise ConfigError(f"missing required key '{key}'")
    try:
        dtype = DriveType(str(data["type"]).upper())
    except ValueError:
        raise ConfigError(f"type must be PROTECTED or SIMPLE, got '{data['type']}'") from None
    outer = _validate_uuid("outer_uuid", data["outer_uuid"])
    inner = data.get("inner_uuid")
    if dtype is DriveType.PROTECTED:
        if inner is None:
            raise ConfigError("PROTECTED drives require inner_uuid")
        inner = _validate_uuid("inner_uuid", inner)
        if inner == outer:
            raise ConfigError("inner_uuid must differ from outer_uuid")
    elif inner is not None:
        raise ConfigError("inner_uuid is only valid for PROTECTED drives")
    for key in ("hint", "fstype"):
        if key in data and not isinstance(data[key], str):
            raise ConfigError(f"{key} must be a string")
    opts = data.get("mount_options", [])
    if not isinstance(opts, list) or not all(isinstance(o, str) and o and "," not in o and " " not in o for o in opts):
        raise ConfigError("mount_options must be a list of single, non-empty option strings")
    if "force" in opts:
        raise ConfigError("'force' is forbidden (clears the NTFS dirty bit -> data loss under Windows)")
    links = data.get("symlinks", [])
    if not isinstance(links, list) or not all(isinstance(s, str) and s for s in links):
        raise ConfigError("symlinks must be a list of non-empty strings")
    if not isinstance(data["mountpoint"], str) or not data["mountpoint"]:
        raise ConfigError("mountpoint must be a non-empty string")
    mountpoint = resolve_configured_path(data["mountpoint"], resolve_symlinks=True)
    if mountpoint == Path("/") or mountpoint == Path.home():
        raise ConfigError("mountpoint may not be / or $HOME")
    symlinks = tuple(resolve_configured_path(s, resolve_symlinks=False) for s in links)
    if mountpoint in symlinks:
        raise ConfigError("a symlink cannot point to itself (symlink == mountpoint)")
    return Drive(name, dtype, mountpoint, outer, inner, data.get("hint"), data.get("fstype"), tuple(opts), symlinks)


def load_config(override: Path | None) -> dict[str, Drive]:
    if override is not None:
        if not override.is_file():
            err(f"Config file '{override}' not found.")
            sys.exit(EXIT_FAIL)
        target = override
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".config"
        candidates = [base / "drive_manager" / "drives.toml", Path(__file__).resolve().parent / "drives.toml"]
        target = next((p for p in candidates if p.is_file()), None)
        if target is None:
            err("drives.toml not found. Searched: " + " , ".join(map(str, candidates)))
            sys.exit(EXIT_FAIL)
    try:
        with open(target, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        err(f"TOML parse error in {target}: {e}")
        sys.exit(EXIT_FAIL)
    except OSError as e:
        err(f"Cannot read {target}: {e.strerror}")
        sys.exit(EXIT_FAIL)
    top_unknown = set(raw) - {"drives"}
    if top_unknown:
        err(f"Config error: unknown top-level key(s) {', '.join(sorted(top_unknown))} in {target}")
        sys.exit(EXIT_FAIL)
    entries = raw.get("drives")
    if not isinstance(entries, dict) or not entries:
        err(f"Config error: no [drives.<name>] tables in {target}")
        sys.exit(EXIT_FAIL)
    drives: dict[str, Drive] = {}
    for name, data in entries.items():
        try:
            drives[name] = parse_drive(name, data)
        except ConfigError as e:
            err(f"Config error in [drives.{name}]: {e}")
            sys.exit(EXIT_FAIL)
    # cross-drive collisions
    seen: dict[str, str] = {}
    for d in drives.values():
        for label, key in (("outer_uuid", d.outer_uuid), ("inner_uuid", d.inner_uuid), ("mountpoint", str(d.mountpoint)),
                           *((f"symlink {s}", str(s)) for s in d.symlinks)):
            if key is None:
                continue
            k = f"{label.split()[0]}::{key}"
            if k in seen and seen[k] != d.name:
                err(f"Config error: {label} '{key}' is shared by drives '{seen[k]}' and '{d.name}'")
                sys.exit(EXIT_FAIL)
            seen[k] = d.name
        for s in d.symlinks:
            for other in drives.values():
                if other.mountpoint == s:
                    err(f"Config error: symlink '{s}' of drive '{d.name}' collides with the mountpoint of '{other.name}'")
                    sys.exit(EXIT_FAIL)
    return drives


# ------------------------------------------------------------------------------
#  MAIN
# ------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drive_manager.py",
        description="Universal Drive Manager — Platinum Hybrid Edition (parallel LUKS/BitLocker fleet control)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-c", "--config", type=Path, help="Path to drives.toml (overrides XDG lookup)")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="Live fleet table (no privileges required)")
    p = sub.add_parser("unlock", help="Decrypt (if needed) and mount drive(s)")
    p.add_argument("targets", nargs="+", metavar="DRIVE")
    p = sub.add_parser("lock", help="Unmount everywhere and close container(s)")
    p.add_argument("targets", nargs="+", metavar="DRIVE")
    p = sub.add_parser("set-password", help="Store a passphrase in the Secret Service keyring")
    p.add_argument("targets", nargs="+", metavar="DRIVE")
    return parser


def main() -> int:
    prevent_root_execution()
    args = build_parser().parse_args()
    check_dependencies()
    drives = load_config(args.config)

    match args.action:
        case "status":
            show_status(drives)
            return EXIT_OK

        case "set-password":
            ok = True
            for i, name in enumerate(args.targets):
                if i:
                    console.print("\n[dim]" + "-" * 60 + "[/dim]\n")
                if not set_password(drives, name):
                    ok = False
                    err(f"Keyring setup for '{name}' failed.")
            return EXIT_OK if ok else EXIT_FAIL

        case "unlock" | "lock" as action:
            unknown = [t for t in args.targets if t not in drives]
            if unknown:
                err(f"Unknown drive(s): {', '.join(unknown)}. Configured: {', '.join(sorted(drives))}")
                return EXIT_FAIL
            targets = list(dict.fromkeys(args.targets))   # de-duplicate, keep order
            Sudo.prime()
            Sudo.start_keepalive()
            acquire_lock()
            accel = CPUAccelerator()

            def _on_signal(signum: int, _frame) -> None:
                accel.restore()
                sys.exit(128 + signum)

            signal.signal(signal.SIGTERM, _on_signal)
            signal.signal(signal.SIGHUP, _on_signal)
            with accel:
                if action == "unlock":
                    return unlock_pipeline(drives, targets)
                return lock_pipeline(drives, targets)

        case _:
            return EXIT_FAIL


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        cancel_exit()
