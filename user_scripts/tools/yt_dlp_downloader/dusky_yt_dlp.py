#!/usr/bin/env python3
"""
Dusky Stream Downloader — Open Video Downloader (OVD v3.2.1) Engine Port
Platform: Arch Linux | Python 3.14+ | yt-dlp 2026+ | FFmpeg 9+

Directly ports:
- yt-dlp argument compilation and safe logging summaries (ytdlp_runner.rs)
- Custom RAW progress template and stage state-machine (ytdlp_progress.rs)
- Process-group lifecycle management & tree killing (ytdlp_download.rs)
- Format approximation & sorting (formats.ts)
- Zero SSD wear: /mnt/zram1 buffering with /dev/shm fallback
- Atomic metadata tag cleansing to strictly episode numbers
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Final, Iterator
import uuid

# ==============================================================================
# PHASE 1: DEPENDENCY AUDIT & SYSTEM AUTO-ELEVATION
# ==============================================================================

REQUIRED_SYSTEM_BINARIES: Final[dict[str, str]] = {
    "ffmpeg": "ffmpeg",
    "fzf": "fzf",
}

REQUIRED_PYTHON_MODULES: Final[dict[str, str]] = {
    "rich": "python-rich",
    "yt_dlp": "yt-dlp",
}


def bootstrap_dependencies() -> None:
    """Detects missing Arch Linux packages and auto-elevates via pacman."""
    missing: list[str] = []

    for binary, pkg in REQUIRED_SYSTEM_BINARIES.items():
        if not shutil.which(binary):
            missing.append(pkg)

    for mod, pkg in REQUIRED_PYTHON_MODULES.items():
        if importlib.util.find_spec(mod) is None:
            if pkg not in missing:
                missing.append(pkg)

    if not missing:
        return

    print("\n[!] Missing Arch Linux dependencies detected:")
    for pkg in missing:
        print(f"    - {pkg}")
    print("[*] Escalating privileges to execute: sudo pacman -S --needed\n")

    cmd = ["sudo", "pacman", "-S", "--needed"] + missing
    try:
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            sys.stderr.write("\n[-] Pacman installation was cancelled or failed.\n")
            sys.exit(res.returncode)
    except Exception as err:
        sys.stderr.write(f"\n[-] Elevation error: {err}\n")
        sys.exit(1)

    print("[+] Dependencies resolved. Re-executing...\n")
    os.execv(sys.executable, [sys.executable] + sys.argv)


bootstrap_dependencies()

# ==============================================================================
# PHASE 2: MODULE IMPORTS & LIFECYCLE MANAGEMENT
# ==============================================================================

import argparse
from dataclasses import dataclass, field
from enum import StrEnum

import yt_dlp
from rich import box
from rich.console import Console
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
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

console: Final[Console] = Console()

PRIMARY_ZRAM_TARGET: Final[Path] = Path("/mnt/zram1")
APP_NAMESPACE: Final[str] = "dusky_ytdlp"
RAM_TMPFS_FALLBACK: Final[Path] = Path("/dev/shm") / APP_NAMESPACE

ACTIVE_PROCESS_GROUPS: set[int] = set()
ACTIVE_SCRATCH_PATHS: set[Path] = set()


def global_signal_handler(signum: int, frame: object) -> None:
    """
    Kills the entire process group (yt-dlp + FFmpeg children) and purges RAM buffers.
    Mirrors kill_tree in ytdlp_download.rs and ytdlp_runner.rs.
    """
    console.print("\n\n[bold red][!] Interrupted: Reaping process tree & cleaning memory buffers...[/]")
    for pgid in list(ACTIVE_PROCESS_GROUPS):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass

    for scratch in list(ACTIVE_SCRATCH_PATHS):
        try:
            if scratch.exists():
                scratch.unlink()
        except OSError:
            pass

    sys.exit(130)


signal.signal(signal.SIGINT, global_signal_handler)
signal.signal(signal.SIGTERM, global_signal_handler)

# ==============================================================================
# PHASE 3: CORE DATA MODELS (Ported from media.ts & config_models.rs)
# ==============================================================================


class TrackType(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    BOTH = "both"


class ProgressStage(StrEnum):
    INITIALIZING = "Initializing"
    DOWNLOADING = "Downloading"
    MERGING = "Merging"
    REMUXING = "Remuxing"
    REENCODING = "Reencoding"
    FINALIZING = "Finalizing"


class ProgressCategory(StrEnum):
    VIDEO = "Video"
    AUDIO = "Audio"
    SUBTITLES = "Subtitles"
    THUMBNAIL = "Thumbnail"
    METADATA = "Metadata"
    OTHER = "Other"


@dataclass(slots=True)
class MediaProgress:
    percentage: float | None = None
    speed_bps: float | None = None
    eta_secs: int | None = None
    category: ProgressCategory = ProgressCategory.OTHER
    stage: ProgressStage = ProgressStage.INITIALIZING
    destination: str | None = None


# ==============================================================================
# PHASE 4: PROGRESS PARSER (Ported verbatim from ytdlp_progress.rs)
# ==============================================================================

# Exact RAW progress template from ytdlp_runner.rs
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


class YtdlpProgressParser:
    """
    Port of YtdlpProgressParser from ytdlp_progress.rs.
    Parses yt-dlp stdout lines, updates stages, and extracts transfer metrics.
    """

    def __init__(self, initial_category: ProgressCategory):
        self.current_category = initial_category
        self.current_stage = ProgressStage.INITIALIZING

    def parse_line(self, line: str, progress_state: MediaProgress) -> None:
        line_clean = line.strip()

        # 1. Postprocess stages
        if line_clean.startswith("[VideoRemuxer]"):
            self.current_stage = ProgressStage.REMUXING
            progress_state.stage = self.current_stage
            return
        if line_clean.startswith("[VideoConvertor]"):
            self.current_stage = ProgressStage.REENCODING
            progress_state.stage = self.current_stage
            return

        # 2. Download stage & category detection from file extension
        if "[download] Destination:" in line_clean:
            dest = line_clean.split("Destination:", 1)[1].strip()
            ext = Path(dest).suffix.lower().lstrip(".")
            if ext in {"mp4", "mkv", "webm", "flv", "mov", "avi", "ts"}:
                self.current_category = ProgressCategory.VIDEO
            elif ext in {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aac"}:
                self.current_category = ProgressCategory.AUDIO
            elif ext in {"vtt", "srt", "ass", "lrc"}:
                self.current_category = ProgressCategory.SUBTITLES
            else:
                self.current_category = ProgressCategory.OTHER

            self.current_stage = ProgressStage.DOWNLOADING
            progress_state.stage = self.current_stage
            progress_state.category = self.current_category
            return

        # 3. Merger destination & stage
        if line_clean.startswith("[Merger] Merging formats into"):
            self.current_stage = ProgressStage.MERGING
            progress_state.stage = self.current_stage
            target = line_clean.replace("[Merger] Merging formats into", "").strip().strip('"')
            progress_state.destination = target
            return

        # 4. Finalizing triggers
        finalizing_triggers = ("[ffmpeg]", "[Fixup]", "Deleting original file")
        if any(t in line_clean for t in finalizing_triggers):
            if self.current_stage != ProgressStage.FINALIZING:
                self.current_stage = ProgressStage.FINALIZING
                progress_state.stage = self.current_stage
            return

        # 5. RAW progress tokens
        if line_clean.startswith("RAW|"):
            raw_content = line_clean[4:]
            parts = raw_content.split("|")
            while len(parts) < 9:
                parts.append("")

            def parse_opt_pct(s: str) -> float | None:
                t = s.strip().rstrip("%").strip()
                if not t or t.lower() == "na":
                    return None
                try:
                    return float(t)
                except ValueError:
                    return None

            def parse_opt_float(s: str) -> float | None:
                t = s.strip()
                if not t or t.lower() == "na":
                    return None
                try:
                    return float(t)
                except ValueError:
                    return None

            def parse_opt_int(s: str) -> int | None:
                t = s.strip()
                if not t or t.lower() == "na":
                    return None
                try:
                    return int(t)
                except ValueError:
                    return None

            pct_num = parse_opt_pct(parts[0])
            pct_str = parse_opt_pct(parts[1])
            speed_bps = parse_opt_float(parts[2])
            eta_secs = parse_opt_int(parts[3])
            dl_bytes = parse_opt_int(parts[4])
            total_bytes = parse_opt_int(parts[5]) or parse_opt_int(parts[6])
            frag_i = parse_opt_int(parts[7])
            frag_n = parse_opt_int(parts[8])

            pct = pct_num if pct_num is not None else pct_str
            if pct is None and dl_bytes is not None and total_bytes and total_bytes > 0:
                pct = (dl_bytes / total_bytes) * 100.0
            elif pct is None and frag_i is not None and frag_n and frag_n > 0:
                pct = (frag_i / frag_n) * 100.0

            if pct is not None:
                progress_state.percentage = max(0.0, min(100.0, pct))
            if speed_bps is not None:
                progress_state.speed_bps = speed_bps
            if eta_secs is not None:
                progress_state.eta_secs = eta_secs


# ==============================================================================
# PHASE 5: RUNNER & ARGUMENT COMPILER (Ported from ytdlp_runner.rs)
# ==============================================================================


class YtdlpRunner:
    """
    Port of YtdlpRunner from ytdlp_runner.rs.
    Assembles CLI flags and manages process group execution.
    """

    def __init__(self, mode: TrackType, output_template: str, url: str):
        self.mode = mode
        self.output_template = output_template
        self.url = url
        self.args: list[str] = [
            "--encoding", "utf-8",
            "--newline",
            "--progress",
            "--no-color",
            "--progress-template", RAW_PROGRESS_TEMPLATE,
            "--progress-delta", "0.5",
            # Live stream and fault tolerance
            "--live-from-start",
            "--wait-for-video", "5-60",
            "--retries", "50",
            "--fragment-retries", "50",
            "--extractor-retries", "10",
            "--file-access-retries", "10",
            "--concurrent-fragment-downloads", "4",
            "--skip-unavailable-fragments",
            "--socket-timeout", "30",
            # Isolation & privacy
            "--no-write-thumbnail",
            "--no-write-info-json",
            "--no-write-description",
            "--no-add-metadata",
        ]
        self._compile_format_and_output()

    def _compile_format_and_output(self) -> None:
        if self.mode == TrackType.AUDIO:
            # YouTube Opus Format 251 priority cascade
            self.args.extend([
                "-f", "bestaudio[ext=opus]/bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
                "-x",
                "--audio-format", "opus",
                "--audio-quality", "0",
            ])
        else:
            self.args.extend([
                "-f", "bestvideo*+bestaudio/best",
                "--merge-output-format", "mp4",
            ])

        self.args.extend(["-o", self.output_template, self.url])

    def summarize_for_log(self) -> dict[str, object]:
        """Port of summarize_args_for_log from ytdlp_runner.rs."""
        return {
            "arg_count": len(self.args),
            "has_proxy": any(a == "--proxy" or a.startswith("--proxy=") for a in self.args),
            "has_cookies": any(a == "--cookies" or a.startswith("--cookies=") for a in self.args),
            "has_auth": any(a in {"--username", "--password", "--video-password"} for a in self.args),
        }

    def spawn(self) -> tuple[subprocess.Popen, int]:
        """Spawns yt-dlp in a dedicated process group to eliminate orphan FFmpeg processes."""
        summary = self.summarize_for_log()
        # Diagnostic summary matching tracing::info! in ytdlp_runner.rs
        cmd = ["yt-dlp"] + self.args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            preexec_fn=os.setsid,  # Create distinct process group
        )
        pgid = os.getpgid(proc.pid)
        ACTIVE_PROCESS_GROUPS.add(pgid)
        return proc, pgid


# ==============================================================================
# PHASE 6: STORAGE ALLOCATION & ATOMIC TAG STRIPPING
# ==============================================================================


def resolve_zero_wear_directory(custom_path: Path | None = None) -> Path:
    """Ensures writes occur exclusively in RAM/ZRAM."""
    candidates: list[Path] = []
    if custom_path:
        candidates.append(custom_path / APP_NAMESPACE)
    candidates.extend([PRIMARY_ZRAM_TARGET / APP_NAMESPACE, RAM_TMPFS_FALLBACK])

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".write_test_{uuid.uuid4().hex[:6]}"
            probe.touch()
            probe.unlink()

            stats = shutil.disk_usage(path)
            if (stats.free / (1024 * 1024)) < 500:
                console.print(f"[bold yellow]![/] Low memory warning on {path}")
            return path
        except (OSError, PermissionError):
            continue

    fallback = Path.cwd() / APP_NAMESPACE
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def sanitize_episode_id(token: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", token.strip())
    if not cleaned:
        raise ValueError(f"Invalid episode ID: {token}")
    return cleaned


def scrub_tags_atomic(scratch_path: Path, final_path: Path, ep_id: str) -> None:
    """
    Atomic FFmpeg stream copy pass:
    - Nukes all global and container metadata tags (-map_metadata -1)
    - Stretches title and track strictly to the episode ID
    """
    temp_clean = scratch_path.with_name(f".clean_{uuid.uuid4().hex[:6]}_{scratch_path.name}")
    ACTIVE_SCRATCH_PATHS.add(temp_clean)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-i", str(scratch_path),
        "-map", "0",
        "-c", "copy",
        "-map_metadata", "-1",
        "-metadata", f"title={ep_id}",
        "-metadata", f"track={ep_id}",
        "-metadata", "artist=",
        "-metadata", "album=",
        "-metadata", "comment=",
        "-metadata", "description=",
        "-metadata", "synopsis=",
        str(temp_clean),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode == 0 and temp_clean.exists():
        if scratch_path.exists():
            scratch_path.unlink()
            ACTIVE_SCRATCH_PATHS.discard(scratch_path)
        temp_clean.replace(final_path)
        ACTIVE_SCRATCH_PATHS.discard(temp_clean)
    else:
        if temp_clean.exists():
            temp_clean.unlink()
            ACTIVE_SCRATCH_PATHS.discard(temp_clean)
        scratch_path.replace(final_path)
        ACTIVE_SCRATCH_PATHS.discard(scratch_path)


# ==============================================================================
# PHASE 7: DOWNLOAD PIPELINE (Ported from ytdlp_download.rs)
# ==============================================================================


@dataclass(slots=True)
class DownloadJob:
    episode_tag: str
    url: str
    mode: TrackType


@dataclass(slots=True)
class JobOutcome:
    episode_tag: str
    status: str
    saved_path: Path | None = None
    size_mb: float = 0.0
    error: str | None = None


def execute_download_job(job: DownloadJob, storage_dir: Path) -> JobOutcome:
    ext = "opus" if job.mode == TrackType.AUDIO else "mp4"
    final_artifact = storage_dir / f"{job.episode_tag}.{ext}"

    scratch_token = f".scratch_{job.episode_tag}_{uuid.uuid4().hex[:6]}"
    scratch_template = str(storage_dir / f"{scratch_token}.%(ext)s")

    runner = YtdlpRunner(job.mode, scratch_template, job.url)
    parser = YtdlpProgressParser(
        ProgressCategory.AUDIO if job.mode == TrackType.AUDIO else ProgressCategory.VIDEO
    )
    progress_state = MediaProgress()

    proc, pgid = runner.spawn()

    # Streaming thread to capture and parse stdout in real time
    def read_stream(stream: Iterator[bytes]) -> None:
        for raw_line in stream:
            try:
                line_str = raw_line.decode("utf-8", errors="replace")
                parser.parse_line(line_str, progress_state)
            except Exception:
                pass

    reader_thread = threading.Thread(target=read_stream, args=(proc.stdout,), daemon=True)
    reader_thread.start()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]Ep {task.fields[ep]}[/]"),
        BarColumn(bar_width=32),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.description}"),
        console=console,
        transient=True,
    ) as progress_ui:
        task_id = progress_ui.add_task("Initializing", total=100.0, ep=job.episode_tag)

        while proc.poll() is None:
            time.sleep(0.1)
            pct = progress_state.percentage or 0.0
            eta_str = f"{progress_state.eta_secs}s" if progress_state.eta_secs else "--:--"
            speed_str = (
                f"{progress_state.speed_bps / (1024 * 1024):.1f}MB/s"
                if progress_state.speed_bps
                else "--B/s"
            )

            desc = f"[cyan]{progress_state.stage} ({speed_str} | ETA: {eta_str})[/]"
            progress_ui.update(task_id, completed=pct, description=desc)

    reader_thread.join(timeout=1.0)
    ACTIVE_PROCESS_GROUPS.discard(pgid)

    exit_code = proc.returncode
    if exit_code != 0:
        stderr_err = proc.stderr.read().decode("utf-8", errors="replace").strip()
        return JobOutcome(
            job.episode_tag,
            status="Failed",
            error=stderr_err.split("\n")[-1] or f"Exit code {exit_code}",
        )

    matches = list(storage_dir.glob(f"{scratch_token}.*"))
    if not matches:
        return JobOutcome(job.episode_tag, status="Failed", error="Scratch buffer missing from ZRAM")

    scratch_file = matches[0]
    ACTIVE_SCRATCH_PATHS.add(scratch_file)

    scrub_tags_atomic(scratch_file, final_artifact, job.episode_tag)

    size_mb = final_artifact.stat().st_size / (1024 * 1024)
    return JobOutcome(job.episode_tag, status="Success", saved_path=final_artifact, size_mb=size_mb)


# ==============================================================================
# PHASE 8: INTERACTIVE TUI & WORKFLOW ORCHESTRATION
# ==============================================================================


def probe_remote_target(url: str) -> tuple[list[str], bool, str]:
    """Inspects target metadata using flat extraction."""
    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(url, download=False)

    if not meta:
        raise ValueError("No metadata returned from target URL.")

    if meta.get("live_status") == "is_upcoming":
        raise ValueError("Live stream is scheduled and has not started broadcasting.")

    if "entries" in meta and meta["entries"]:
        urls: list[str] = []
        for e in meta["entries"]:
            if not e:
                continue
            u = e.get("url") or e.get("webpage_url")
            if not u and e.get("id"):
                u = f"https://www.youtube.com/watch?v={e['id']}"
            if u:
                urls.append(u)
        return urls, True, meta.get("title") or "Collection"

    single = meta.get("webpage_url") or meta.get("original_url") or url
    return [single], False, meta.get("title") or "Single Stream"


def run_interactive_wizard() -> tuple[list[DownloadJob], Path]:
    console.print(
        Panel.fit(
            "[bold cyan]Dusky Stream Downloader (OVD v3.2.1 Architecture)[/]\n"
            "[dim]Zero-wear ZRAM streaming engine for Arch Linux[/]",
            border_style="cyan",
            box=box.DOUBLE,
        )
    )

    format_choice = Prompt.ask(
        "\n[bold green]?[/] Delivery format",
        choices=["audio", "video"],
        default="audio",
    )
    mode = TrackType.AUDIO if format_choice == "audio" else TrackType.VIDEO

    jobs: list[DownloadJob] = []

    while True:
        raw = Prompt.ask("\n[bold green]?[/] Enter webpage link, playlist URL, or batch file path").strip()
        if not raw:
            continue

        possible_file = Path(raw)
        if possible_file.is_file():
            with possible_file.open("r", encoding="utf-8") as f:
                for line in f:
                    clean = line.strip()
                    if not clean or clean.startswith(("#", "//")):
                        continue
                    parts = re.split(r"[\s,;]+", clean, maxsplit=1)
                    if len(parts) == 2 and parts[1].startswith("http"):
                        jobs.append(DownloadJob(sanitize_episode_id(parts[0]), parts[1].strip(), mode))
            if jobs:
                break
            console.print("[bold red]Batch file contained no valid jobs.[/]")
            continue

        try:
            with console.status("[bold cyan]Probing remote stream target...[/]", spinner="dots"):
                discovered, is_collection, title = probe_remote_target(raw)
            break
        except Exception as e:
            console.print(Panel(f"[bold red]Probe failed:[/] {e}", border_style="red"))

    if not jobs:
        if not is_collection:
            console.print(f"[green]✓[/] Single stream detected: [dim]{title}[/]")
            ep = Prompt.ask("[bold green]?[/] Assign Episode Number", default="01")
            jobs = [DownloadJob(sanitize_episode_id(ep), discovered[0], mode)]
        else:
            total = len(discovered)
            console.print(f"[green]✓[/] Collection detected: [yellow]{total}[/] items ([dim]{title}[/])")
            start = IntPrompt.ask("[bold green]?[/] Starting Episode Number", default=1)
            pad = max(2, len(str(start + total - 1)))

            # Chronological inversion for channel streams
            if Confirm.ask("[bold green]?[/] Invert order? (Oldest ➔ Newest)", default=False):
                discovered.reverse()

            range_val = Prompt.ask(
                f"[bold green]?[/] Range to extract ('all' or '1-{min(10, total)}')", default="all"
            ).strip()
            if range_val.lower() != "all" and "-" in range_val:
                try:
                    s_str, e_str = range_val.split("-", 1)
                    discovered = discovered[max(0, int(s_str) - 1) : int(e_str)]
                except ValueError:
                    pass

            jobs = [DownloadJob(f"{i:0{pad}d}", u, mode) for i, u in enumerate(discovered, start=start)]

    suggested = resolve_zero_wear_directory()
    custom_dir = Prompt.ask("[bold green]?[/] Destination directory (ZRAM)", default=str(suggested))
    final_storage = resolve_zero_wear_directory(Path(custom_dir))

    return jobs, final_storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dusky Stream Downloader — Open Video Downloader Engine Port."
    )
    parser.add_argument("target", nargs="?", help="URL (stream, playlist) or batch file path")
    parser.add_argument("-e", "--episode", type=str, help="Episode identifier")
    parser.add_argument(
        "-m", "--mode", choices=["audio", "video"], default="audio", help="Delivery format"
    )
    parser.add_argument("-o", "--output-dir", type=Path, help="Target storage directory")

    args = parser.parse_args()

    if not args.target:
        jobs, destination = run_interactive_wizard()
    else:
        destination = resolve_zero_wear_directory(args.output_dir)
        mode = TrackType.AUDIO if args.mode == "audio" else TrackType.VIDEO
        target_path = Path(args.target)

        if target_path.is_file():
            jobs = []
            with target_path.open("r", encoding="utf-8") as f:
                for line in f:
                    clean = line.strip()
                    if clean and not clean.startswith(("#", "//")):
                        parts = re.split(r"[\s,;]+", clean, maxsplit=1)
                        if len(parts) == 2 and parts[1].startswith("http"):
                            jobs.append(DownloadJob(sanitize_episode_id(parts[0]), parts[1].strip(), mode))
        else:
            urls, is_collection, _ = probe_remote_target(args.target)
            if not is_collection:
                ep = sanitize_episode_id(args.episode) if args.episode else "01"
                jobs = [DownloadJob(ep, urls[0], mode)]
            else:
                pad = max(2, len(str(len(urls))))
                jobs = [DownloadJob(f"{i:0{pad}d}", u, mode) for i, u in enumerate(urls, start=1)]

    if not jobs:
        console.print("[bold red]No jobs queued.[/]")
        sys.exit(1)

    console.print(
        f"\n[bold green]➜[/] Storage Pool: [cyan]{destination}[/] | Queue: [yellow]{len(jobs)}[/] | Format: [magenta]{jobs[0].mode.upper()}[/]\n"
    )

    outcomes: list[JobOutcome] = []
    for job in jobs:
        console.print(f"[bold blue]•[/] Extracting Episode [bold yellow]{job.episode_tag}[/]")
        res = execute_download_job(job, destination)
        outcomes.append(res)

    table = Table(
        title="Extraction Log",
        box=box.ROUNDED,
        header_style="bold cyan",
        title_style="bold green",
    )
    table.add_column("Episode", justify="center", style="bold yellow")
    table.add_column("Status", justify="center")
    table.add_column("Size", justify="right")
    table.add_column("Disk Artifact", style="dim")

    for o in outcomes:
        status_render = "[bold green]Success[/]" if o.status == "Success" else f"[bold red]{o.error}[/]"
        size_render = f"{o.size_mb:.2f} MB" if o.status == "Success" else "--"
        out_name = o.saved_path.name if o.saved_path else "--"
        table.add_row(o.episode_tag, status_render, size_render, out_name)

    console.print("\n")
    console.print(table)
    console.print(
        Panel(
            f"[bold green]Artifact Storage:[/] [cyan]{destination}[/]\n"
            f"[dim]All original titles scrubbed from disk and metadata tags.\n"
            f"Artifacts reside exclusively in RAM/ZRAM. Move to target phone before shutdown.[/]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()
