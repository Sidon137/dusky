#!/usr/bin/env python3
"""
Dusky Universal Media Downloader
Platform: Arch Linux (rolling) | Python 3.14+ | yt-dlp 2026.08+ | FFmpeg 9+
Architecture: Port of Open Video Downloader (OVD) engine — TUI edition.

Verified against (Sep 2026):
- yt-dlp 2026.08.19 (`--progress-template` RAW protocol, `-S` format-sort,
  `--merge-output-format`/`--remux-video`, `--embed-metadata`/`--embed-chapters`)
- FFmpeg n9.0.1 (mp4 muxer defaults: h264 video / aac audio)
- Python 3.14.7 (`subprocess.Popen(process_group=0)` == setpgid(0,0) isolation)

Storage policy: writes exclusively to /mnt/zram1/dusky_ytdlp with /dev/shm
fallback. No username is ever hardcoded; paths are absolute system paths.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Final
import uuid

# ==============================================================================
# PHASE 1: DEPENDENCY VERIFICATION & ARCH LINUX AUTO-ELEVATION
# ==============================================================================

# fzf is OPTIONAL (used for picker UX when present, never required).
REQUIRED_SYSTEM_BINARIES: Final[dict[str, str]] = {
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffmpeg",  # ffprobe ships inside the Arch `ffmpeg` package
}

REQUIRED_PYTHON_MODULES: Final[dict[str, str]] = {
    "rich": "python-rich",
    "yt_dlp": "yt-dlp",
}

MIN_PYTHON: Final[tuple[int, int]] = (3, 14)


def bootstrap_dependencies() -> None:
    """Detects missing Arch Linux packages and auto-elevates via pacman."""
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            f"[-] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
            f"found {sys.version.split()[0]}.\n"
        )
        sys.exit(1)

    missing: list[str] = []

    for binary, pkg in REQUIRED_SYSTEM_BINARIES.items():
        if shutil.which(binary) is None and pkg not in missing:
            missing.append(pkg)

    for mod, pkg in REQUIRED_PYTHON_MODULES.items():
        if importlib.util.find_spec(mod) is None and pkg not in missing:
            missing.append(pkg)

    if not missing:
        return

    print("\n[!] Missing system packages detected on Arch Linux:")
    for pkg in missing:
        print(f"    - {pkg}")
    print("[*] Escalating privileges to execute: sudo pacman -S --needed\n")

    cmd = ["sudo", "pacman", "-S", "--needed", *missing]
    try:
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            sys.stderr.write("\n[-] Pacman installation was cancelled or failed.\n")
            sys.exit(res.returncode)
    except Exception as err:
        sys.stderr.write(f"\n[-] Elevation error: {err}\n")
        sys.exit(1)

    print("[+] Dependencies resolved. Initializing script environment...\n")
    os.execv(sys.executable, [sys.executable, *sys.argv])


bootstrap_dependencies()

# ==============================================================================
# PHASE 2: MODULE IMPORTS & LIFECYCLE MANAGEMENT
# ==============================================================================

import argparse
from dataclasses import dataclass
from enum import StrEnum

import yt_dlp
from rich import box
from rich.align import Align
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

console: Final[Console] = Console()

PRIMARY_ZRAM_TARGET: Final[Path] = Path("/mnt/zram1/dusky_ytdlp")
RAM_TMPFS_FALLBACK: Final[Path] = Path("/dev/shm/dusky_ytdlp")

ACTIVE_PROCESS_GROUPS: set[int] = set()
_ACTIVE_PG_LOCK: Final[threading.Lock] = threading.Lock()


def global_signal_handler(signum: int, frame: object) -> None:
    """Kills the entire process group (yt-dlp + FFmpeg children) on SIGINT/SIGTERM."""
    console.print("\n\n[bold red][!] Interrupted: Terminating process tree...[/]")
    with _ACTIVE_PG_LOCK:
        pgids = list(ACTIVE_PROCESS_GROUPS)
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    sys.exit(130)


signal.signal(signal.SIGINT, global_signal_handler)
signal.signal(signal.SIGTERM, global_signal_handler)


def has_fzf() -> bool:
    return shutil.which("fzf") is not None


def fzf_pick(prompt: str, choices: list[str], default: str) -> str:
    """Single-select via fzf when interactive, else Rich Prompt. Never crashes.

    fzf requires a real TTY; with piped/redirected stdin (or no TTY at all)
    it would silently return the default WITHOUT consuming stdin, shifting
    every subsequent wizard prompt. Gate on isatty so scripts/pipes behave.

    No pre-filled query: all options are shown and Enter selects the
    highlighted (first) entry.
    """
    if has_fzf() and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            proc = subprocess.run(
                ["fzf", "--prompt", f"{prompt}> ", "--height", "40%",
                 "--reverse", "--no-multi"],
                input="\n".join(choices) + "\n",
                capture_output=True,
                text=True,
                timeout=120,
            )
            picked = (proc.stdout or "").strip()
            if picked and picked in choices:
                return picked
            # User escaped fzf (non-zero) -> fall through to default
            if not picked:
                return default
        except Exception:
            pass
    return Prompt.ask(
        f"\n[bold green]?[/] {prompt}",
        choices=choices,
        default=default,
    )


# ==============================================================================
# PHASE 3: CORE DATA MODELS
# ==============================================================================


class TargetFormat(StrEnum):
    VIDEO = "video"
    AUDIO_OPUS = "audio-opus"
    AUDIO_MP3 = "audio-mp3"
    AUDIO_BEST = "audio-best"


# Wizard order: audio-best sits on top so plain Enter picks it.
FORMAT_CHOICES: Final[list[str]] = ["audio-best", "audio-opus", "audio-mp3", "video"]
DEFAULT_FORMAT: Final[str] = "audio-best"

# Standard video caps offered in the quality picker.
QUALITY_CAPS: Final[list[int]] = [2160, 1440, 1080, 720, 480, 360]
QUALITY_LABELS: Final[dict[int, str]] = {
    2160: "2160p · 4K",
    1440: "1440p · QHD",
    1080: "1080p · Full HD",
    720: "720p · HD",
}


def format_duration(total_secs: float | int | None) -> str:
    if total_secs is None:
        return "--:--"
    secs = int(total_secs)
    hours, secs = divmod(max(secs, 0), 3600)
    mins, secs = divmod(secs, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def quality_label(cap: int | None, available_max: int | None = None) -> str:
    if cap is None:
        suffix = f" (up to {available_max}p)" if available_max else ""
        return f"Best available{suffix}"
    return QUALITY_LABELS.get(cap, f"{cap}p")


def build_quality_choices(heights: list[int]) -> list[tuple[str, int | None]]:
    """Quality picker entries from the heights a link actually offers.

    Falls back to the full standard cap list when the link exposes nothing
    (probe failed, audio-only source, playlist/batch context).
    """
    available_max = max(heights) if heights else None
    caps = [c for c in QUALITY_CAPS if available_max is None or c <= available_max]
    if available_max is not None and not caps:
        caps = [available_max]
    choices = [(quality_label(None, available_max), None)]
    choices.extend((quality_label(c), c) for c in caps)
    return choices


class ProgressStage(StrEnum):
    INITIALIZING = "Initializing"
    DOWNLOADING = "Downloading"
    MERGING = "Merging"
    REMUXING = "Remuxing"
    REENCODING = "Reencoding"
    FINALIZING = "Finalizing"


@dataclass(slots=True)
class MediaProgress:
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    percentage: float = 0.0
    speed_bps: float | None = None
    eta_secs: int | None = None
    stage: ProgressStage = ProgressStage.INITIALIZING
    destination_file: str | None = None
    already_archived: bool = False


# ==============================================================================
# PHASE 4: OVD STREAMING PROGRESS PARSER (Ported from ytdlp_progress.rs)
# ==============================================================================

RAW_PROGRESS_TEMPLATE: Final[str] = (
    "RAW|"
    "%(progress.percent|)s|"
    "%(progress._percent_str|)s|"
    "%(progress.speed|)s|"
    "%(progress.eta|)s|"
    "%(progress.downloaded_bytes|)s|"
    "%(progress.total_bytes|)s|"
    "%(progress.total_bytes_estimate|)s|"
    "%(progress.fragment_index|)s|"
    "%(progress.fragment_count|)s"
)


def _parse_opt_int(raw: str) -> int | None:
    """Parse ints that yt-dlp may render as floats ('125952.0')."""
    t = raw.strip()
    if not t or t.lower() == "na":
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return int(float(t))
    except ValueError:
        return None


def _parse_opt_float(raw: str) -> float | None:
    t = raw.strip()
    if not t or t.lower() == "na":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_opt_pct(raw: str) -> float | None:
    t = raw.strip().removesuffix("%").strip()
    if not t or t.lower() == "na":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _clamp01pct(value: float) -> float:
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf guard
        return 0.0
    return max(0.0, min(100.0, value))


class YtdlpProgressParser:
    """Parses yt-dlp stdout lines and tracks byte counts and stages in real time.

    Faithful port of OVD's `try_progress_update`: percentage is derived from
    (1) `progress.percent`, else (2) `progress._percent_str`, else (3) bytes
    ratio, else (4) fragment ratio — clamped to [0, 100]. Live yt-dlp output
    (verified Sep 2026) always leaves field 0 empty and renders totals/ETA as
    floats, so float-tolerant parsing is mandatory.
    """

    def __init__(self) -> None:
        self.current_stage = ProgressStage.INITIALIZING

    def parse_line(self, line: str, progress_state: MediaProgress) -> None:
        line_clean = line.strip()
        if not line_clean:
            return

        # 1. Postprocess stages (check before generic destination handling)
        if line_clean.startswith("[VideoRemuxer]"):
            self.current_stage = ProgressStage.REMUXING
            progress_state.stage = self.current_stage
            dest = self._extract_destination(line_clean)
            if dest:
                progress_state.destination_file = dest
            return
        if line_clean.startswith("[VideoConvertor]"):
            self.current_stage = ProgressStage.REENCODING
            progress_state.stage = self.current_stage
            dest = self._extract_destination(line_clean)
            if dest:
                progress_state.destination_file = dest
            return

        # 2. Already-downloaded / already-archived fast paths (no bytes arrive)
        if "has already been recorded in the archive" in line_clean:
            progress_state.already_archived = True
            if self.current_stage != ProgressStage.FINALIZING:
                self.current_stage = ProgressStage.FINALIZING
                progress_state.stage = self.current_stage
            return

        if "has already been downloaded" in line_clean:
            self.current_stage = ProgressStage.FINALIZING
            progress_state.stage = self.current_stage
            # "[download] /path/file.mp4 has already been downloaded"
            rest = line_clean.split("[download]", 1)[-1]
            path_part = rest.split("has already been downloaded", 1)[0].strip()
            if path_part:
                progress_state.destination_file = Path(path_part).name
                if progress_state.total_bytes:
                    progress_state.downloaded_bytes = progress_state.total_bytes
                    progress_state.percentage = 100.0
            return

        # 3. Destination tracking
        if "[download] Destination:" in line_clean:
            self.current_stage = ProgressStage.DOWNLOADING
            progress_state.stage = self.current_stage
            dest = line_clean.split("Destination:", 1)[1].strip()
            progress_state.destination_file = Path(dest).name
            return

        if line_clean.startswith("[Merger] Merging formats into"):
            self.current_stage = ProgressStage.MERGING
            progress_state.stage = self.current_stage
            target = line_clean.replace("[Merger] Merging formats into", "").strip().strip('"')
            progress_state.destination_file = Path(target).name
            return

        if line_clean.startswith("[ExtractAudio]"):
            dest = self._extract_destination(line_clean)
            if dest:
                progress_state.destination_file = dest
            return

        # 4. Finalizing triggers
        if any(t in line_clean for t in ("[ffmpeg]", "[Fixup]", "Deleting original file")):
            if self.current_stage != ProgressStage.FINALIZING:
                self.current_stage = ProgressStage.FINALIZING
                progress_state.stage = self.current_stage
            return

        # 5. RAW progress protocol metrics
        if line_clean.startswith("RAW|"):
            self._parse_raw(line_clean, progress_state)

    @staticmethod
    def _extract_destination(line_clean: str) -> str | None:
        if "Destination:" not in line_clean:
            return None
        dest = line_clean.split("Destination:", 1)[1].strip().strip('"')
        return Path(dest).name if dest else None

    def _parse_raw(self, line_clean: str, progress_state: MediaProgress) -> None:
        parts = line_clean[4:].split("|")
        while len(parts) < 9:
            parts.append("")

        pct_num = _parse_opt_pct(parts[0])
        pct_str = _parse_opt_pct(parts[1])
        speed = _parse_opt_float(parts[2])
        eta_raw = _parse_opt_float(parts[3])
        eta = int(eta_raw) if eta_raw is not None and eta_raw >= 0 else None
        dl_bytes = _parse_opt_int(parts[4])
        total_bytes = _parse_opt_int(parts[5])
        estimate = _parse_opt_int(parts[6])
        frag_i = _parse_opt_int(parts[7])
        frag_n = _parse_opt_int(parts[8])

        if dl_bytes is not None:
            progress_state.downloaded_bytes = dl_bytes
        resolved_total = total_bytes or estimate
        if resolved_total is not None and resolved_total > 0:
            progress_state.total_bytes = resolved_total
        if speed is not None:
            progress_state.speed_bps = speed
        if eta is not None:
            progress_state.eta_secs = eta

        # Percentage derivation mirrors OVD: explicit -> bytes ratio -> fragments.
        pct: float | None = pct_num if pct_num is not None else pct_str
        if pct is None:
            total = progress_state.total_bytes
            if total and total > 0 and progress_state.downloaded_bytes is not None:
                pct = (progress_state.downloaded_bytes / total) * 100.0
        if pct is None and frag_i is not None and frag_n:
            pct = (frag_i / frag_n) * 100.0
        if pct is not None:
            progress_state.percentage = _clamp01pct(pct)


# ==============================================================================
# PHASE 5: STORAGE MANAGEMENT & RUNNER COMPILER
# ==============================================================================


def config_state_dir() -> Path | None:
    """Persistent state dir: ~/.config/dusky/settings/dusky_ytdlp (XDG-aware).

    Lives on real disk (NOT zram) so resume/skip state survives reboots.
    No username is hardcoded — resolved from $XDG_CONFIG_HOME or $HOME.
    Returns None if the directory cannot be created (caller runs stateless).
    """
    try:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        state_dir = base / "dusky" / "settings" / "dusky_ytdlp"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    except (OSError, PermissionError, RuntimeError):
        return None


def download_archive_path(mode: TargetFormat) -> Path | None:
    """Per-format yt-dlp download-archive file (the resume/skip state file).

    One archive per delivery format so grabbing audio of a link never marks
    its video as done (and vice versa). yt-dlp records `extractor id` lines
    here and skips them on later runs — re-running a batch continues where
    it stopped instead of re-downloading.
    """
    state_dir = config_state_dir()
    return state_dir / f"archive-{mode.value}.txt" if state_dir else None


def resolve_storage_pool(custom_path: Path | None = None) -> Path:
    """Ensures media writes occur strictly in memory (ZRAM or tmpfs)."""
    candidates = [custom_path] if custom_path else [PRIMARY_ZRAM_TARGET, RAM_TMPFS_FALLBACK]

    for path in candidates:
        if path is None:
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".probe_{uuid.uuid4().hex[:6]}"
            probe.touch()
            probe.unlink()

            stats = shutil.disk_usage(path)
            if (stats.free / (1024 * 1024)) < 500:
                console.print(f"[bold yellow]![/] Warning: Storage pool {path} has under 500 MB remaining.")
            return path
        except (OSError, PermissionError):
            continue

    fallback = Path.cwd() / "dusky_downloads"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


class YtdlpRunner:
    """Compiles yt-dlp arguments and manages isolated process execution."""

    def __init__(self, mode: TargetFormat, output_dir: Path, url: str, max_height: int | None = None):
        self.mode = mode
        self.output_dir = output_dir
        self.url = url
        self.max_height = max_height

        # Keeps original title; `%(title).180B` byte-truncates for 255B FAT32/
        # eCryptfs limits. `--windows-filenames` + `--trim-filenames 180`
        # provide defense-in-depth for MTP/Android transfers.
        output_template = str(output_dir / "%(title).180B [%(id)s].%(ext)s")

        self.args: list[str] = [
            "--encoding", "utf-8",
            "--newline",
            "--progress",
            "--no-color",
            "--progress-template", RAW_PROGRESS_TEMPLATE,
            "--progress-delta", "0.5",
            # Our queue expands playlists manually -> each job must be single.
            "--no-playlist",
            # Multi-connection & fragment recovery (verified flags, yt-dlp 2026.08)
            "--concurrent-fragments", "4",
            "--retries", "30",
            "--fragment-retries", "30",
            "--file-access-retries", "10",
            "--retry-sleep", "fragment:exp=1:20",
            "--socket-timeout", "30",
            # Replace invalid FAT32/Android characters for safe phone transfer
            "--windows-filenames",
            "--trim-filenames", "180",
            "--output-na-placeholder", "",
            # Preserve embedded metadata tags + chapters (canonical 2026 flags;
            # `--add-metadata` is merely an alias of `--embed-metadata`)
            "--embed-metadata",
            "--embed-chapters",
        ]
        # Resume/skip state: per-format download archive on persistent disk.
        # Re-running the same URLs/batch skips finished items and continues
        # where the queue stopped. Omitted only if no state dir is writable.
        archive = download_archive_path(mode)
        if archive is not None:
            self.args.extend(["--download-archive", str(archive)])
        self._compile_format(output_template)

    def _compile_format(self, output_template: str) -> None:
        match self.mode:
            case TargetFormat.AUDIO_OPUS:
                self.args.extend([
                    "-f", "bestaudio[ext=opus]/bestaudio[acodec=opus]/bestaudio/best",
                    "-x", "--audio-format", "opus", "--audio-quality", "0",
                ])
            case TargetFormat.AUDIO_MP3:
                self.args.extend([
                    "-f", "bestaudio/best",
                    "-x", "--audio-format", "mp3", "--audio-quality", "0",
                ])
            case TargetFormat.AUDIO_BEST:
                # `--audio-format best` keeps the native best audio stream
                # without transcoding (verified: `best` is the documented
                # default/no-op conversion target).
                self.args.extend([
                    "-f", "bestaudio/best",
                    "-x", "--audio-format", "best",
                ])
            case TargetFormat.VIDEO:
                if self.max_height is not None:
                    cap = self.max_height
                    selector = (
                        f"bv*[height<={cap}]+ba/b[height<={cap}]"
                        f"/bv*[height<={cap}]+ba/b"
                    )
                else:
                    # `bv*+ba/b` prefers separate AV streams (highest quality);
                    # `-S` biases toward phone-compatible H.264/AAC-in-MP4
                    # without hard-failing when only VP9/AV1 exists.
                    selector = "bv*+ba/b"
                self.args.extend([
                    "-f", selector,
                    "-S", "vcodec:h264,acodec:aac,vext:mp4,lang,quality,res,fps,hdr:12",
                    "--merge-output-format", "mp4",
                    "--remux-video", "mp4",
                ])

        self.args.extend(["-o", output_template, self.url])

    def spawn(self) -> tuple[subprocess.Popen, int]:
        """Spawns yt-dlp in a distinct process group (setpgid(0,0)).

        `process_group=0` (Python 3.11+) is the modern equivalent of the OVD
        Rust `pre_exec(setpgid(0,0))`: the child becomes its own group leader
        (pgid == pid), so `os.killpg` reaps yt-dlp + FFmpeg children together.
        stdin is DEVNULL so yt-dlp can never block on an interactive prompt;
        both stdout AND stderr are piped (stderr must be drained concurrently
        to avoid 64 KiB pipe-buffer deadlock).
        """
        cmd = ["yt-dlp", *self.args]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # never allow interactive prompts to hang
            process_group=0,  # isolate process tree
        )
        pgid = os.getpgid(proc.pid)
        with _ACTIVE_PG_LOCK:
            ACTIVE_PROCESS_GROUPS.add(pgid)
        return proc, pgid


# ==============================================================================
# PHASE 6: DOWNLOAD PIPELINE
# ==============================================================================


@dataclass(slots=True)
class MediaJob:
    title: str
    url: str
    mode: TargetFormat
    max_height: int | None = None


@dataclass(slots=True)
class JobReport:
    title: str
    status: str
    saved_file: str = "--"
    size_mb: float = 0.0
    error: str | None = None


_BATCH_COMMENT_PREFIXES: Final[tuple[str, ...]] = ("#", ";", "]", "//")


def parse_batch_file(path: Path) -> list[str]:
    """Parse a yt-dlp-style batch file: one URL per line.

    Lines starting with `#`, `;` or `]` are comments (yt-dlp convention);
    `//` is also accepted. Blank lines are skipped.
    """
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            clean = line.strip()
            if not clean or clean.startswith(_BATCH_COMMENT_PREFIXES):
                continue
            urls.append(clean)
    return urls


def execute_download(
    job: MediaJob,
    output_dir: Path,
    *,
    timeout_secs: float | None = None,
) -> JobReport:
    runner = YtdlpRunner(job.mode, output_dir, job.url, job.max_height)
    parser = YtdlpProgressParser()
    progress_state = MediaProgress()
    started_ns = time.monotonic_ns()

    try:
        proc, pgid = runner.spawn()
    except FileNotFoundError:
        return JobReport(title=job.title, status="Failed", error="yt-dlp binary not found in PATH")
    except Exception as err:
        return JobReport(title=job.title, status="Failed", error=str(err))

    assert proc.stdout is not None and proc.stderr is not None
    stderr_lines: list[str] = []
    stderr_lock = threading.Lock()

    def drain_stream(stream: object, is_stdout: bool) -> None:
        # Reads byte-by-byte splits on \\n/\\r so `--newline` progress lines
        # and \\r-style FFmpeg updates are both handled without blocking.
        buf = bytearray()
        read1 = getattr(stream, "read", None)
        try:
            while True:
                chunk = read1(1) if callable(read1) else None
                if not chunk:
                    break
                byte = chunk[0] if isinstance(chunk, (bytes, bytearray)) else ord(chunk)
                if byte in (10, 13):  # \\n or \\r
                    if buf:
                        text = bytes(buf).decode("utf-8", errors="replace")
                        del buf[:]
                        if is_stdout:
                            try:
                                parser.parse_line(text, progress_state)
                            except Exception:
                                pass
                        else:
                            try:
                                parser.parse_line(text, progress_state)
                            except Exception:
                                pass
                            with stderr_lock:
                                stderr_lines.append(text)
                                if len(stderr_lines) > 200:
                                    del stderr_lines[: len(stderr_lines) - 200]
                    continue
                buf.append(byte)
        except Exception:
            pass
        finally:
            if buf:
                text = bytes(buf).decode("utf-8", errors="replace")
                try:
                    parser.parse_line(text, progress_state)
                except Exception:
                    pass
                if not is_stdout:
                    with stderr_lock:
                        stderr_lines.append(text)

    stdout_thread = threading.Thread(target=drain_stream, args=(proc.stdout, True), daemon=True)
    stderr_thread = threading.Thread(target=drain_stream, args=(proc.stderr, False), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    display_title = (job.title[:30] + "..") if len(job.title) > 32 else job.title

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold yellow]{task.fields[title]}[/]"),
            BarColumn(bar_width=24),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            TextColumn("[bold cyan]{task.description}[/]"),
            console=console,
            transient=True,
        ) as progress_ui:
            task_id = progress_ui.add_task("Initializing", total=None, title=display_title)

            while proc.poll() is None:
                if timeout_secs is not None and (time.monotonic_ns() - started_ns) / 1e9 > timeout_secs:
                    with _ACTIVE_PG_LOCK:
                        pass
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except OSError:
                        pass
                    proc.wait(timeout=10)
                    break
                # Prefer byte-accurate totals; fall back to OVD-derived % for
                # fragment-only (HLS) streams where no total exists.
                if progress_state.total_bytes:
                    completed = progress_state.downloaded_bytes
                    total: float | None = float(progress_state.total_bytes)
                elif progress_state.percentage > 0:
                    completed = progress_state.percentage
                    total = 100.0
                else:
                    completed = progress_state.downloaded_bytes
                    total = None
                progress_ui.update(
                    task_id,
                    completed=completed,
                    total=total,
                    description=f"[{progress_state.stage}]",
                )
                time.sleep(0.1)
    finally:
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        with _ACTIVE_PG_LOCK:
            ACTIVE_PROCESS_GROUPS.discard(pgid)

    exit_code = proc.returncode if proc.returncode is not None else 1
    if exit_code != 0:
        with stderr_lock:
            tail = [ln for ln in stderr_lines if ln.strip()][-3:]
        if tail:
            err_msg = " | ".join(ln.strip()[:300] for ln in tail)
        else:
            err_msg = f"yt-dlp error code {exit_code}"
        # Translate the cryptic ffprobe failure: the source simply carries no
        # audio track (e.g. a video-only clip), so no audio mode can succeed.
        # Say so plainly instead of leaking postprocessor internals.
        if job.mode != TargetFormat.VIDEO and "unable to obtain file audio codec" in err_msg:
            err_msg += " — source has no audio track; retry with -f video"
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.stderr.close()
        except Exception:
            pass
        return JobReport(title=job.title, status="Failed", error=err_msg)

    try:
        proc.stdout.close()
    except Exception:
        pass
    try:
        proc.stderr.close()
    except Exception:
        pass

    # Archive skip: yt-dlp recorded this id on an earlier run — nothing to
    # locate on disk (it may have been a different directory), so report the
    # honest Skipped state instead of attributing a stranger's file to it.
    if progress_state.already_archived:
        return JobReport(
            title=job.title, status="Skipped", saved_file="--", error="already in archive",
        )

    # Locate output: prefer parser-tracked destination; else the newest file
    # created after this job started (never an unrelated older file).
    dest_file = progress_state.destination_file or "--"
    size_mb = 0.0
    actual_path: Path | None = None
    if dest_file != "--":
        actual_path = output_dir / dest_file
        if not actual_path.exists():
            actual_path = _newest_file_since(output_dir, started_ns) or actual_path
            if actual_path is not None and actual_path.exists():
                dest_file = actual_path.name
    else:
        actual_path = _newest_file_since(output_dir, started_ns)
        if actual_path is not None:
            dest_file = actual_path.name

    if actual_path is not None and actual_path.exists():
        # Fix trailing space before extension: e.g. "title .m4a" -> "title.m4a"
        clean_name = re.sub(r"\s+\.([a-zA-Z0-9]+)$", r".\1", actual_path.name)
        if clean_name != actual_path.name:
            cleaned_path = actual_path.with_name(clean_name)
            try:
                actual_path.replace(cleaned_path)
            except OSError:
                pass
            else:
                actual_path = cleaned_path
                dest_file = clean_name

        try:
            size_mb = actual_path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0

    return JobReport(title=job.title, status="Success", saved_file=dest_file, size_mb=size_mb)


def _newest_file_since(directory: Path, started_ns: int) -> Path | None:
    """Newest regular file in `directory` modified after `started_ns`."""
    newest: Path | None = None
    newest_mtime = -1.0
    started_s = started_ns / 1e9 - 1.0  # 1s grace for clock skew
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file() or entry.name.startswith(".probe_"):
                continue
            if entry.suffix == ".part" or entry.suffix == ".ytdl":
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= started_s and mtime > newest_mtime:
            newest = entry
            newest_mtime = mtime
    # Fallback: if nothing newer (e.g. "already downloaded" fast path),
    # return the most recent finished file rather than nothing.
    if newest is None:
        try:
            finished = [
                p for p in directory.iterdir()
                if p.is_file() and not p.name.startswith(".probe_")
                and p.suffix not in (".part", ".ytdl")
            ]
        except OSError:
            return None
        if finished:
            try:
                newest = max(finished, key=lambda p: p.stat().st_mtime)
            except OSError:
                return None
    return newest


# ==============================================================================
# PHASE 7: TARGET PROBING & INTERACTIVE TUI
# ==============================================================================


def probe_media_target(url: str) -> tuple[list[tuple[str, str]], bool, str]:
    """Universal flat extraction probe across any media endpoint."""
    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 10,
        "ignoreerrors": "only_download",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("No metadata returned by extractor.")

    entries = info.get("entries")
    if entries:
        items: list[tuple[str, str]] = []
        for e in entries:
            if not e:
                continue
            item_url = e.get("url") or e.get("webpage_url")
            if not item_url:
                # Flat YouTube entries carry a bare video id; rebuild a
                # directly-downloadable watch URL instead of passing the id.
                vid = e.get("id")
                if vid and e.get("ie_key") == "Youtube":
                    item_url = f"https://www.youtube.com/watch?v={vid}"
                elif vid:
                    item_url = vid
            if item_url:
                items.append((e.get("title") or item_url, item_url))
        if not items:
            raise ValueError("Playlist contained no downloadable entries.")
        if len(items) > 500:
            console.print(
                f"[bold yellow]![/] Large collection: {len(items)} items. "
                "Consider a narrow range to save time/RAM."
            )
        return items, True, info.get("title") or "Collection / Feed"

    single_url = info.get("webpage_url") or info.get("original_url") or url
    single_title = info.get("title") or single_url
    return [(single_title, single_url)], False, single_title


@dataclass(slots=True)
class VideoDetails:
    title: str
    duration_secs: int | None
    uploader: str | None
    heights: list[int]


def probe_video_details(url: str) -> VideoDetails | None:
    """Full-metadata inspect of a single video: duration, uploader, heights.

    Returns None on any failure (caller falls back to generic options).
    Playlists/collections are never inspected here — pass.
    """
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info or info.get("_type") == "playlist":
        return None
    heights = sorted(
        {
            f.get("height")
            for f in (info.get("formats") or [])
            if isinstance(f.get("height"), int) and f.get("height")
        },
        reverse=True,
    )
    raw_dur = info.get("duration")
    duration = int(raw_dur) if isinstance(raw_dur, (int, float)) else None
    uploader = info.get("uploader") or info.get("channel") or info.get("extractor_key")
    return VideoDetails(
        title=info.get("title") or url,
        duration_secs=duration,
        uploader=str(uploader) if uploader else None,
        heights=heights,
    )


def select_playlist_items(
    discovered: list[tuple[str, str]],
    range_val: str,
) -> list[tuple[str, str]]:
    """Select items using yt-dlp `-I` flavour syntax.

    Supports: `all`, `5`, `1-3`, `1:5`, `1:10:2`, comma lists (`1,3,5-7`),
    negatives (`-3:` = last three). Out-of-range indices are ignored;
    an empty selection raises ValueError.
    """
    total = len(discovered)
    spec = range_val.strip().lower()
    if spec in ("all", "*"):
        return list(discovered)

    picked: list[int] = []  # 0-based
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Normalise legacy `a-b` to canonical `a:b`
        if "-" in chunk and ":" not in chunk:
            a, _, b = chunk.partition("-")
            chunk = f"{a}:{b}"
        try:
            idxs = _expand_item_spec(chunk, total)
        except ValueError:
            continue
        for i in idxs:
            if i not in picked:
                picked.append(i)

    selected = [discovered[i] for i in sorted(picked) if 0 <= i < total]
    if not selected:
        raise ValueError(f"No items matched range {range_val!r} (playlist has {total}).")
    return selected


def _expand_item_spec(chunk: str, total: int) -> list[int]:
    """Expand one `START[:STOP[:STEP]]` chunk (1-based, inclusive STOP)."""
    if ":" not in chunk:  # single index
        n = int(chunk)
        i = n - 1 if n > 0 else total + n
        if not 0 <= i < total:
            raise ValueError("out of range")
        return [i]
    parts = chunk.split(":")
    if len(parts) > 3:
        raise ValueError("bad spec")
    while len(parts) < 3:
        parts.append("")
    start_s, stop_s, step_s = (p.strip() for p in parts)
    step = int(step_s) if step_s else 1
    if step == 0:
        raise ValueError("step cannot be 0")
    # Convert 1-based inclusive bounds to 0-based exclusive slice bounds.
    if step > 0:
        start = int(start_s) - 1 if start_s else 0
        stop = int(stop_s) if stop_s else total
        if start_s and int(start_s) < 0:
            start = total + int(start_s)
        if stop_s and int(stop_s) < 0:
            stop = total + int(stop_s) + 1
    else:
        start = (int(start_s) - 1 if start_s else total - 1)
        stop = (int(stop_s) - 2 if stop_s else -total - 1)
        if start_s and int(start_s) < 0:
            start = total + int(start_s)
        if stop_s and int(stop_s) < 0:
            stop = total + int(stop_s) - 1
        # Build reversed range manually below.
        out: list[int] = []
        i = start
        while (i > stop) if step < 0 else (i < stop):
            if 0 <= i < total:
                out.append(i)
            i += step
        return out
    return [i for i in range(start, stop, step) if 0 <= i < total]


_COMMA_BEFORE_URL: Final[re.Pattern] = re.compile(r",\s*(?=https?://)", re.IGNORECASE)


def split_url_list(raw: str) -> list[str]:
    """Split pasted multi-URL input on commas that introduce a new URL.

    Only commas directly preceding `http(s)://` act as separators, so commas
    embedded inside a single URL are preserved. Surrounding whitespace and
    empty fragments are dropped.
    """
    return [frag.strip() for frag in _COMMA_BEFORE_URL.split(raw) if frag.strip()]


def collect_targets(
    raw_urls: list[str], playlist_items: str = "all"
) -> tuple[list[tuple[str, str]], list[str]]:
    """Probe each URL independently; one bad link never kills the queue.

    Playlists expand per URL (range-filtered); singles pass through. Returns
    (items, errors) so callers can warn yet continue with what resolved.
    """
    items: list[tuple[str, str]] = []
    errors: list[str] = []
    for url in raw_urls:
        try:
            found, is_collection, _ = probe_media_target(url)
            if is_collection:
                try:
                    found = select_playlist_items(found, playlist_items)
                except ValueError as err:
                    errors.append(f"{url}: {err}")
                    continue
            items.extend(found)
        except Exception as err:
            errors.append(f"{url}: {err}")
    return items, errors


def run_interactive_wizard() -> tuple[list[MediaJob], Path]:
    console.print(
        Panel.fit(
            Align.center("[bold cyan]Dusky YT-DLP[/]"),
            border_style="cyan",
            box=box.DOUBLE,
        ),
        justify="center",
    )

    # 1. Link(s) first — everything else depends on what they point at.
    #    Accepts one link, several comma-separated links, a playlist, or a
    #    batch file. Each link is probed on its own: one dead URL never kills
    #    the rest of the queue.
    batch_urls: list[str] | None = None
    discovered: list[tuple[str, str]] = []
    is_collection = False
    label = ""
    multi_mode = False

    while True:
        raw_target = Prompt.ask(
            "\n[bold green]?[/] Enter media link(s), playlist URL, or batch file path"
            "\n[dim]Several links? Separate them with commas.[/]"
        ).strip()
        if not raw_target:
            continue

        raw_urls = split_url_list(raw_target)
        if len(raw_urls) == 1 and Path(raw_urls[0]).expanduser().is_file():
            local_file = Path(raw_urls[0]).expanduser()
            try:
                urls = parse_batch_file(local_file)
            except OSError as err:
                console.print(f"[bold red]Cannot read batch file:[/] {err}")
                continue
            if urls:
                batch_urls = urls
                break
            console.print("[bold red]Batch file contained no valid URLs.[/]")
            continue

        if len(raw_urls) == 1:
            try:
                with console.status("[bold cyan]Probing remote endpoint...[/]", spinner="dots"):
                    discovered, is_collection, label = probe_media_target(raw_urls[0])
                break
            except Exception as err:
                console.print(Panel(f"[bold red]Probe failed:[/] {escape(str(err))}", border_style="red"))
            continue

        multi_mode = True
        with console.status(f"[bold cyan]Probing {len(raw_urls)} links...[/]", spinner="dots"):
            discovered, probe_errors = collect_targets(raw_urls)
        if probe_errors:
            console.print(
                Panel(
                    "[bold yellow]Some links failed (skipped):[/]\n"
                    + "\n".join(escape(e) for e in probe_errors),
                    border_style="yellow",
                )
            )
        if discovered:
            console.print(
                f"[green]✓[/] Resolved [yellow]{len(discovered)}[/] item(s) "
                f"from [yellow]{len(raw_urls)}[/] link(s)."
            )
            break
        console.print("[bold red]No link resolved to anything downloadable.[/]")

    # 2. Show what the link(s) actually offer, then offer matching options.
    details: VideoDetails | None = None
    if batch_urls is None:
        if multi_mode:
            console.print(
                Panel(
                    f"[yellow]{len(discovered)}[/] item(s) queued — best match "
                    "is picked per link at download time.",
                    title="[green]Sources[/]",
                    border_style="green",
                )
            )
        elif not is_collection:
            title, link = discovered[0]
            with console.status("[bold cyan]Inspecting available formats...[/]", spinner="dots"):
                details = probe_video_details(link)
            show_title = details.title if details else title
            meta_bits: list[str] = []
            if details and details.uploader:
                meta_bits.append(details.uploader)
            if details:
                meta_bits.append(format_duration(details.duration_secs))
            if details and details.heights:
                meta_bits.append(f"up to {details.heights[0]}p")
            meta_line = f"\n[dim]{escape(' · '.join(meta_bits))}[/]" if meta_bits else ""
            console.print(
                Panel(
                    f"[bold yellow]{escape(show_title)}[/]{meta_line}",
                    title="[green]Source[/]",
                    border_style="green",
                )
            )
        else:
            total = len(discovered)
            console.print(
                Panel(
                    f"[bold yellow]{escape(label)}[/]\n[dim]{total} items[/]",
                    title="[green]Collection[/]",
                    border_style="green",
                )
            )

    # 3. Delivery format (audio-best on top; Enter takes it).
    fmt_choice = fzf_pick("Select delivery format", FORMAT_CHOICES, DEFAULT_FORMAT)
    mode = TargetFormat(fmt_choice)

    # 4. Video quality — capped to what the link really provides.
    max_height: int | None = None
    if mode == TargetFormat.VIDEO:
        q_choices = build_quality_choices(details.heights if details else [])
        q_labels = [label for label, _ in q_choices]
        q_pick = fzf_pick("Select video quality", q_labels, q_labels[0])
        max_height = dict(q_choices)[q_pick]

    # 5. Build the queue.
    jobs: list[MediaJob] = []
    if batch_urls is not None:
        jobs = [
            MediaJob(title=f"Batch Item {idx}", url=u, mode=mode, max_height=max_height)
            for idx, u in enumerate(batch_urls, start=1)
        ]
        console.print(f"[green]✓[/] Queued [yellow]{len(jobs)}[/] item(s) from batch file.")
    elif multi_mode:
        jobs = [MediaJob(title=item[0], url=item[1], mode=mode, max_height=max_height) for item in discovered]
        console.print(f"[green]✓[/] Queued [yellow]{len(jobs)}[/] item(s).")
    elif not is_collection:
        title, link = discovered[0]
        jobs = [MediaJob(title=details.title if details else title, url=link, mode=mode, max_height=max_height)]
    else:
        total = len(discovered)
        if total > 1 and Confirm.ask("[bold green]?[/] Invert order? (Oldest ➔ Newest)", default=False):
            discovered.reverse()

        while True:
            range_val = Prompt.ask(
                "[bold green]?[/] Range ('all', '5', '1-3' or '1:10:2')",
                default="all",
            ).strip()
            try:
                picked = select_playlist_items(discovered, range_val)
                break
            except ValueError as err:
                console.print(f"[bold red]{err}[/]")

        jobs = [MediaJob(title=item[0], url=item[1], mode=mode, max_height=max_height) for item in picked]
        console.print(f"[green]✓[/] Queued [yellow]{len(jobs)}[/] item(s).")

    default_dir = resolve_storage_pool()
    custom_dir = Prompt.ask("[bold green]?[/] Target directory (ZRAM)", default=str(default_dir))
    destination = resolve_storage_pool(Path(custom_dir).expanduser())
    if destination != Path(custom_dir).expanduser():
        console.print(f"[bold yellow]![/] Requested path unusable; using [cyan]{destination}[/].")

    return jobs, destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Dusky Universal Media Downloader.")
    parser.add_argument(
        "target",
        nargs="*",
        help="URL(s), comma-separated URLs, playlist(s) and/or batch file(s). "
             "Each link is probed on its own; one bad link never kills the queue.",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["video", "audio-opus", "audio-mp3", "audio-best"],
        default="audio-best",
        help="Delivery format (default: audio-best)",
    )
    parser.add_argument(
        "-q",
        "--quality",
        choices=["best", "2160", "1440", "1080", "720", "480", "360"],
        default="best",
        help="Video quality cap (default: best). Applies to -f video only.",
    )
    parser.add_argument("-o", "--output-dir", type=Path, help="Storage directory override")
    parser.add_argument(
        "-I", "--playlist-items",
        default="all",
        help="Playlist selection: 'all', '5', '1-3', '1:10:2', '1,3,5-7' (default: all)",
    )

    args = parser.parse_args()

    if not args.target:
        jobs, destination = run_interactive_wizard()
    else:
        destination = resolve_storage_pool(args.output_dir.expanduser() if args.output_dir else None)
        mode = TargetFormat(args.format)
        max_height = None if args.quality == "best" else int(args.quality)
        if max_height is not None and mode != TargetFormat.VIDEO:
            console.print("[bold yellow]![/] --quality only applies to video; ignoring.")
            max_height = None

        # Mixed bag allowed: batch files expand in place, everything else is
        # split on commas (only before http(s)://, so in-URL commas survive).
        batch_urls: list[str] = []
        link_urls: list[str] = []
        for raw_arg in args.target:
            arg_path = Path(raw_arg).expanduser()
            if arg_path.is_file():
                try:
                    batch_urls.extend(parse_batch_file(arg_path))
                except OSError as err:
                    console.print(f"[bold red]Cannot read batch file {raw_arg}:[/] {err}")
            else:
                link_urls.extend(split_url_list(raw_arg))

        discovered: list[tuple[str, str]] = []
        if link_urls:
            with console.status(f"[bold cyan]Probing {len(link_urls)} link(s)...[/]", spinner="dots"):
                found, probe_errors = collect_targets(link_urls, args.playlist_items)
            for err in probe_errors:
                console.print(f"[bold yellow]![/] Skipped: {escape(err)}")
            discovered.extend(found)
        jobs = [MediaJob(title=f"Item {idx}", url=u, mode=mode, max_height=max_height) for idx, u in enumerate(batch_urls, start=1)]
        jobs.extend(
            MediaJob(title=item[0], url=item[1], mode=mode, max_height=max_height)
            for item in discovered
        )
        # Batch entries never got real titles (fast path, no per-URL probe);
        # renumber the combined queue so titles stay unique.
        for idx, job in enumerate(jobs, start=1):
            if job.title.startswith("Item "):
                job.title = f"Item {idx}"

    if not jobs:
        console.print("[bold red]No download targets queued.[/]")
        sys.exit(1)

    fmt_label = jobs[0].mode.upper()
    if jobs[0].mode == TargetFormat.VIDEO and jobs[0].max_height is not None:
        fmt_label += f" ≤{jobs[0].max_height}P"
    console.print(
        f"\n[bold green]➜[/] Storage Pool: [cyan]{destination}[/] | Queue: [yellow]{len(jobs)}[/] | Format: [magenta]{fmt_label}[/]\n"
    )

    reports: list[JobReport] = []
    for job in jobs:
        console.print(f"[bold blue]•[/] Processing: [bold yellow]{escape(job.title)}[/]")
        res = execute_download(job, destination)
        reports.append(res)

    table = Table(
        title="Extraction Log",
        box=box.ROUNDED,
        header_style="bold cyan",
        title_style="bold green",
        expand=True,
    )
    table.add_column("Title", style="yellow", ratio=4, overflow="ellipsis")
    table.add_column("Status", justify="center", width=10)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Filename", style="dim", ratio=5, overflow="ellipsis")

    for r in reports:
        if r.status == "Success":
            status_str = "[bold green]Success[/]"
        elif r.status == "Skipped":
            status_str = "[bold yellow]Skipped[/]"
        else:
            status_str = "[bold red]Failed[/]"
        size_str = f"{r.size_mb:.2f} MB" if r.status == "Success" else "--"
        detail = r.saved_file if r.status == "Success" else (r.error or "failed")
        table.add_row(escape(r.title), status_str, size_str, escape(detail))

    console.print("\n")
    console.print(table)
    console.print(
        Panel(
            f"[bold green]Location:[/] [cyan]{destination}[/]\n"
            f"[dim]Media downloaded with native titles directly into RAM/ZRAM.[/]",
            border_style="green",
        )
    )

    if any(r.status not in ("Success", "Skipped") for r in reports):
        sys.exit(1)


if __name__ == "__main__":
    main()
