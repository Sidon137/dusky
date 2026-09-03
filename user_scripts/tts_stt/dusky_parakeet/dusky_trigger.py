#!/usr/bin/env python3
"""Control client for Dusky STT (stdlib only).

Defaults to toggling realtime dictation with zero arguments (hotkey friendly).
Validates socket ownership/modes before connecting; never trusts permissions alone.
"""

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SERVICE = "dusky_stt.service"
MAX_PACKET = 65536
DEFAULT_TIMEOUT = 10.0

type JsonObject = dict[str, Any]


def control_path() -> Path:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not rt:
        raise RuntimeError("XDG_RUNTIME_DIR is unset.")
    return Path(rt) / "dusky-stt" / "control.sock"


def is_socket_secure(p: Path) -> bool:
    try:
        d_st = p.parent.lstat()
        f_st = p.lstat()
    except OSError:
        return False
    return (d_st.st_uid == os.getuid() and stat.S_IMODE(d_st.st_mode) == 0o700
            and stat.S_ISSOCK(f_st.st_mode) and f_st.st_uid == os.getuid()
            and stat.S_IMODE(f_st.st_mode) == 0o600)


def ensure_service() -> None:
    if is_socket_secure(control_path()):
        return
    subprocess.run(["systemctl", "--user", "start", SERVICE], check=False)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if is_socket_secure(control_path()):
            return
        if subprocess.run(["systemctl", "--user", "is-failed", "--quiet", SERVICE], check=False).returncode == 0:
            break
        time.sleep(0.1)
    raise TimeoutError("Dusky STT socket did not appear; check `dusky_trigger --logs`.")


def send_command(payload: JsonObject, timeout: float = DEFAULT_TIMEOUT) -> JsonObject:
    ensure_service()
    p = control_path()
    if not is_socket_secure(p):
        raise RuntimeError(f"Control socket missing/insecure: {p}")
    blob = json.dumps(payload).encode()
    if len(blob) > MAX_PACKET:
        raise ValueError("Request too large for SEQPACKET")
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC) as s:
        s.settimeout(timeout)
        s.connect(str(p))
        s.sendmsg([blob])
        data, _, flags, _ = s.recvmsg(MAX_PACKET)
        if flags & getattr(socket, "MSG_TRUNC", 0x20):
            raise RuntimeError("Response truncated")
        if not data:
            raise RuntimeError("Daemon closed connection")
    return json.loads(data.decode())


def main() -> int:
    ap = argparse.ArgumentParser(prog="dusky_trigger", description="Control client for Dusky STT")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--toggle", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--file", type=Path, default=None, help="Transcribe audio/video file")
    g.add_argument("--restart", action="store_true")
    g.add_argument("--kill", action="store_true")
    g.add_argument("--logs", action="store_true")
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--realtime", action="store_true", default=False)
    m.add_argument("--push", action="store_true", default=False)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    if args.logs:
        os.execvp("journalctl", ["journalctl", "--user", "-u", SERVICE, "-f", "-o", "short-precise"])
    if args.restart:
        subprocess.run(["systemctl", "--user", "restart", SERVICE], check=True)
        return 0
    if args.kill:
        subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)
        return 0

    mode = "push" if args.push else "realtime"
    if args.status:
        resp = send_command({"command": "status"}, timeout=args.timeout)
    elif args.start:
        resp = send_command({"command": "start", "mode": mode}, timeout=args.timeout)
    elif args.stop:
        resp = send_command({"command": "stop"}, timeout=max(args.timeout, 180.0))
    elif args.file is not None:
        src = args.file.expanduser()
        if not src.is_file():
            print(f"File not found: {src}", file=sys.stderr)
            return 2
        resp = send_command({"command": "file", "path": str(src.resolve())}, timeout=max(args.timeout, 300.0))
    else:  # default: toggle (bare hotkey invocation)
        resp = send_command({"command": "toggle", "mode": mode}, timeout=max(args.timeout, 180.0))

    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        for k, v in resp.items():
            print(f"{k:15}: {v}")
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
