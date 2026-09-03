#!/usr/bin/env python3
"""Dusky STT CPU daemon (hardware-agnostic).

Owns capture, stateful Silero VAD, append-only typing, S1-mini cleanup,
file transcription, and the control plane. ASR runs in an on-demand worker
(.venv-worker) whose EP matches config hardware: CUDA / CPU (+opportunistic
MIGraphX/ROCM on AMD). Audio crosses via sealed memfds over SOCK_SEQPACKET.
"""

import argparse
import collections
import fcntl
import importlib.metadata
import json
import logging
import mmap
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 14, 6)
SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512
VAD_CONTEXT_SAMPLES = 64
BYTES_PER_SAMPLE = 2
MAX_PACKET = 65536
MAX_INLINE = 57344

if sys.version_info < MIN_PYTHON:
    raise SystemExit("Dusky STT requires CPython 3.14.6+")
_gil = getattr(sys, "_is_gil_enabled", None)
if _gil is None or not _gil():
    raise SystemExit("Dusky STT requires GIL-enabled CPython")

# Kernel ABI: Python 3.14 does not expose these on all builds.
if not hasattr(os, "MFD_NOEXEC_SEAL"):
    os.MFD_NOEXEC_SEAL = 0x0008  # type: ignore[attr-defined]
F_SEAL_EXEC = 0x0020

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import numpy as np
import onnxruntime as ort
import sounddevice as sd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s dusky[%(process)d]: %(message)s")
LOG = logging.getLogger("dusky")

APP_DIR = Path(os.environ.get("DUSKY_APP_DIR", Path(__file__).resolve().parent))
CONFIG_PATH = Path(os.environ.get("DUSKY_CONFIG", APP_DIR / "config.json"))

type JsonObject = dict[str, Any]

REQUIRED_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
CUDA_TOKENS = ("libcuda.so", "libcudart.so", "libcublas", "libcudnn", "libnvrtc", "onnxruntime_providers_cuda")
PUNCT = ".,?!:;\"'()[]{}"


def assert_cpu_ort_namespace() -> None:
    owners = sorted(set(importlib.metadata.packages_distributions().get("onnxruntime", [])))
    if owners != ["onnxruntime"]:
        raise RuntimeError(f"CPU ORT namespace not exclusive: {owners}")
    maps = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").casefold()
    for tok in CUDA_TOKENS:
        if tok in maps:
            raise RuntimeError(f"CUDA leaked into CPU daemon: {tok}")


def cuda_maps() -> list[str]:
    try:
        text = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").casefold()
    except OSError:
        return []
    return sorted({tok for tok in CUDA_TOKENS if tok in text})


def systemd_notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.sendto(state.encode(), addr)
    except OSError:
        pass


def watchdog_interval() -> float:
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return 0.0
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid.strip() and int(pid) != os.getpid():
        return 0.0
    try:
        return max(0.25, int(raw) / 2_000_000.0)
    except ValueError:
        return 0.0


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as h:
            h.write(content)
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        tmp.unlink(missing_ok=True)


def create_sealed_audio(pcm: np.ndarray) -> int:
    payload = pcm.astype("<i2", copy=False).tobytes()
    fd = os.memfd_create("dusky-audio", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING | os.MFD_NOEXEC_SEAL)
    try:
        os.ftruncate(fd, len(payload))
        view = memoryview(payload)
        off = 0
        while off < len(payload):
            off += os.pwrite(fd, view[off:], off)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
    except BaseException:
        os.close(fd)
        raise
    return fd


class RingBuffer:
    def __init__(self, capacity_samples: int) -> None:
        self._buf = np.zeros(capacity_samples, dtype="<i2")
        self._cap = capacity_samples
        self._start = 0
        self._len = 0
        self.dropped_samples = 0

    def __len__(self) -> int:
        return self._len

    def reset(self) -> None:
        self._start = 0
        self._len = 0

    def append(self, frame: np.ndarray) -> None:
        count = int(frame.size)
        if count >= self._cap:
            self.dropped_samples += count - self._cap
            self._buf[:] = frame[-self._cap:]
            self._start = 0
            self._len = self._cap
            return
        end = (self._start + self._len) % self._cap
        first = min(count, self._cap - end)
        self._buf[end:end + first] = frame[:first]
        if first < count:
            self._buf[:count - first] = frame[first:]
        ovf = max(0, self._len + count - self._cap)
        if ovf:
            self.dropped_samples += ovf
            self._start = (self._start + ovf) % self._cap
            self._len = self._cap
        else:
            self._len += count

    def read(self, max_samples: int | None = None) -> np.ndarray:
        if self._len == 0:
            return np.empty(0, dtype="<i2")
        first = min(self._len, self._cap - self._start)
        chunks = [self._buf[self._start:self._start + first]]
        if first < self._len:
            chunks.append(self._buf[:self._len - first])
        data = np.concatenate(chunks) if len(chunks) > 1 else chunks[0].copy()
        return data[-max_samples:] if max_samples and data.size > max_samples else data


class StatefulSileroVad:
    def __init__(self, model_path: Path) -> None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, VAD_CONTEXT_SAMPLES), dtype=np.float32)

    def probability(self, pcm: np.ndarray) -> float:
        current = (pcm.astype(np.float32) * (1.0 / 32768.0)).reshape(1, -1)
        model_input = np.concatenate((self._context, current), axis=1)
        out, nxt = self._session.run(None, {
            "input": model_input, "state": self._state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
        self._context = current[:, -VAD_CONTEXT_SAMPLES:].copy()
        self._state = np.asarray(nxt, dtype=np.float32)
        return float(np.asarray(out).reshape(-1)[0])


class WorkerManager:
    def __init__(self, config: JsonObject) -> None:
        self.config = config
        self._cv = threading.Condition(threading.RLock())
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._gen = 0
        self._spawns = 0
        self._inflight: dict[str, int] = {}
        self._results: dict[str, JsonObject] = {}
        self._discarded: set[str] = set()

    @property
    def pid(self) -> int | None:
        with self._cv:
            return self._proc.pid if self._proc and self._proc.poll() is None else None

    def _spawn_locked(self) -> None:
        if self._proc and self._proc.poll() is None and self._sock:
            return
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        for s in (parent, child):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass
        child.set_inheritable(True)
        env = dict(os.environ)
        if str(self.config.get("hardware", "cpu")) == "nvidia":
            env["CUDA_VISIBLE_DEVICES"] = str(self.config.get("gpu_device", 0))
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            env["CUDA_MODULE_LOADING"] = "LAZY"
        else:
            env["CUDA_VISIBLE_DEVICES"] = "-1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["HF_HUB_OFFLINE"] = "1"
        worker_py = APP_DIR / str(self.config.get("worker_python", ".venv-worker/bin/python"))
        worker_script = APP_DIR / str(self.config.get("worker_script", "dusky_worker.py"))
        cfg = APP_DIR / "config.json"
        proc = subprocess.Popen([str(worker_py), str(worker_script), "--config", str(cfg),
                                 "--fd", str(child.fileno())],
                                cwd=APP_DIR, env=env, close_fds=True, pass_fds=(child.fileno(),))
        child.close()
        self._gen += 1
        self._spawns += 1
        self._proc = proc
        self._sock = parent
        threading.Thread(target=self._reader_loop, args=(self._gen, proc, parent),
                         name=f"dusky-worker-{self._gen}", daemon=True).start()
        LOG.info("Spawned worker PID=%d gen=%d hw=%s", proc.pid, self._gen, self.config.get("hardware"))

    def _fail_generation(self, gen: int, reason: str) -> None:
        with self._cv:
            for req_id, g in list(self._inflight.items()):
                if g == gen and req_id not in self._results:
                    self._results[req_id] = {"ok": False, "request_id": req_id, "error": reason}
            self._cv.notify_all()

    def _reader_loop(self, gen: int, proc: subprocess.Popen[bytes], sock: socket.socket) -> None:
        try:
            while True:
                fds: list[int] = []
                try:
                    payload, ancdata, flags, _ = sock.recvmsg(MAX_PACKET, socket.CMSG_SPACE(4 * 8))
                except OSError as exc:
                    LOG.debug("Worker recv failed: %s", exc)
                    break
                for level, ctype, data in ancdata:
                    if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                        n = len(data) // struct.calcsize("i")
                        fds.extend(struct.unpack(f"{n}i", data[:n * struct.calcsize("i")]))
                if flags & getattr(socket, "MSG_CTRUNC", 0x20) or flags & getattr(socket, "MSG_TRUNC", 0x20):
                    for fd in fds:
                        os.close(fd)
                    LOG.warning("Worker packet truncated; discarding generation %d", gen)
                    break
                if len(fds) > 1:
                    for fd in fds:
                        os.close(fd)
                    LOG.warning("Worker sent >1 fd; discarding")
                    continue
                if not payload:
                    for fd in fds:
                        os.close(fd)
                    break
                try:
                    resp = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    for fd in fds:
                        os.close(fd)
                    continue
                if resp.get("payload") == "memfd" and fds:
                    fd = fds[0]
                    try:
                        sz = os.fstat(fd).st_size
                        with mmap.mmap(fd, sz, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ) as m:
                            resp.update(json.loads(m.read().decode("utf-8")))
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        LOG.warning("Bad worker memfd reply: %s", exc)
                    finally:
                        for fd in fds:
                            os.close(fd)
                else:
                    for fd in fds:
                        os.close(fd)
                req_id = resp.get("request_id")
                with self._cv:
                    self._inflight.pop(req_id, None)
                    if req_id in self._discarded:
                        self._discarded.discard(req_id)
                    elif req_id:
                        self._results[req_id] = resp
                    self._cv.notify_all()
        except Exception as exc:
            LOG.debug("Worker channel closed: %s", exc)
        finally:
            self._fail_generation(gen, "worker exited")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            with self._cv:
                if gen == self._gen:
                    self._proc = None
                    self._sock = None
                self._cv.notify_all()
            sock.close()

    def submit(self, pcm: np.ndarray, meta: JsonObject, *, force: bool) -> str | None:
        with self._cv:
            limit = int(self.config.get("max_inflight_requests", 2))
            while len(self._inflight) >= limit:
                if not force:
                    return None
                self._cv.wait(0.1)
            self._spawn_locked()
            assert self._sock is not None
            req_id = uuid.uuid4().hex
            fd = create_sealed_audio(pcm)
            try:
                self._inflight[req_id] = self._gen
                self._sock.sendmsg([json.dumps({"op": "recognize", "request_id": req_id,
                                                "samples": int(pcm.size), "encoding": "s16le", **meta}).encode()],
                                   [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))])
            except OSError:
                self._inflight.pop(req_id, None)
                raise
            finally:
                os.close(fd)
            return req_id

    def wait_result(self, req_id: str, timeout: float) -> JsonObject | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while req_id not in self._results:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    self._discarded.add(req_id)
                    self._inflight.pop(req_id, None)
                    return None
                self._cv.wait(min(rem, 0.2))
            return self._results.pop(req_id)

    def stop(self) -> None:
        with self._cv:
            sock = self._sock
        if sock:
            try:
                sock.sendmsg([b'{"op":"shutdown"}'])
            except OSError:
                pass
        proc = self._proc
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class S1Cleanup:
    SYSTEM = ("You are a text normalizer for speech-to-text transcripts. Output only the cleaned text.")
    def __init__(self, config: JsonObject) -> None:
        self.enabled = bool(config.get("llm_enabled", True))
        self.endpoint = f"{config.get('llm_endpoint', 'http://127.0.0.1:11434')}/api/generate"
        self.model = str(config.get("llm_model", "s1-mini"))
        self.timeout = float(config.get("llm_timeout_seconds", 20.0))
        self.max_tokens = int(config.get("llm_max_tokens", 2048))

    def clean(self, transcript: str) -> str:
        raw = transcript.strip()
        if not self.enabled or not raw:
            return raw
        payload = {"model": self.model, "prompt": raw, "raw": True, "stream": False,
                   "options": {"temperature": 0, "num_predict": self.max_tokens}}
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                cleaned = str(json.loads(resp.read().decode()).get("response", "")).strip()
            if cleaned and "[Styling:" not in cleaned and "<|im_start|>" not in cleaned:
                return cleaned
        except Exception as exc:
            LOG.warning("LLM cleanup bypassed: %s", exc)
        return raw


class StableSuffixTyper:
    def __init__(self, holdback_words: int) -> None:
        self.holdback = max(0, holdback_words)
        self.emitted: list[str] = []
        self.diverged = False
        self.disabled = False

    def reset(self) -> None:
        self.emitted = []
        self.diverged = False

    def update(self, text: str, *, final: bool) -> None:
        if self.diverged or self.disabled:
            return
        words = text.strip().split()
        e_norm = [w.strip(PUNCT).casefold() for w in self.emitted]
        w_norm = [w.strip(PUNCT).casefold() for w in words]
        overlap = 0
        for a, b in zip(e_norm, w_norm):
            if a != b:
                break
            overlap += 1
        if overlap < len(e_norm):
            self.diverged = True
            LOG.warning("Hypothesis diverged; live typing suspended for phrase.")
            return
        target = len(words) if final else max(0, len(words) - self.holdback)
        if target > len(self.emitted):
            chunk = (" " if self.emitted else "") + " ".join(words[len(self.emitted):target])
            try:
                subprocess.run(["wtype", "-"], input=chunk.encode(), check=False, timeout=5)
            except (OSError, subprocess.SubprocessError):
                self.disabled = True
                LOG.warning("wtype failed; live typing disabled for session.")
                return
            self.emitted.extend(words[len(self.emitted):target])


def decode_file_to_pcm(path: Path, chunk_seconds: float) -> list[np.ndarray]:
    """Decode any ffmpeg-readable file to 16k mono s16 chunks. Raises on failure."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
           "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout and proc.stderr
    errs: list[bytes] = []
    def drain() -> None:
        try:
            errs.append(proc.stderr.read() or b"")
        except OSError:
            pass
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    raw = proc.stdout.read()
    t.join(timeout=10)
    rc = proc.wait(timeout=30)
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed ({rc}): {b''.join(errs)[-1000:].decode(errors='replace')}")
    pcm = np.frombuffer(raw, dtype="<i2").copy()
    per = int(chunk_seconds * SAMPLE_RATE)
    return [pcm[i:i + per] for i in range(0, max(1, pcm.size), per)] if pcm.size else []


class RecordingSession:
    def __init__(self, daemon: "DuskyDaemon", realtime: bool) -> None:
        self.daemon = daemon
        self.config = daemon.config
        self.realtime = realtime
        self.session_id = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.vad = StatefulSileroVad(APP_DIR / str(self.config.get("vad_model_path", "models/silero_vad.onnx")))
        cap = int((float(self.config.get("max_phrase_seconds", 15.0)) + 2.0) * SAMPLE_RATE)
        self.ring = RingBuffer(cap)
        self.pre_roll: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, round(float(self.config.get("pre_roll_seconds", 0.32)) * SAMPLE_RATE / VAD_FRAME_SAMPLES)))
        self.typer = StableSuffixTyper(int(self.config.get("stable_holdback_words", 2))) if realtime else None
        self.phrases: list[str] = []
        self.phrase_id = 0
        self._s1 = S1Cleanup(self.config)

    def _finalize_text(self, raw: str) -> str:
        return self._s1.clean(raw) if raw.strip() else ""

    def _publish(self, final_text: str) -> str:
        if not final_text:
            return ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(str(self.config.get("state_dir", "~/.local/state/dusky-stt"))).expanduser() / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out_dir / f"capture-{stamp}-{self.session_id[:8]}.txt", final_text + "\n")
        if not self.realtime and self.config.get("push_type_at_end", True):
            subprocess.run(["wtype", "-"], input=final_text.encode(), check=False)
        subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=final_text.encode(), check=False)
        if self.config.get("notifications", True):
            subprocess.run(["notify-send", "-a", "Dusky STT", "-t", "3500",
                            "Transcription complete", final_text[:220]], check=False)
        return final_text

    def run(self) -> str:
        dev = self.config.get("input_device")
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=VAD_FRAME_SAMPLES, channels=1,
                               dtype="int16", latency="low", device=dev) as stream:
            active = False
            onset = silence = 0
            onset_target = max(1, round(float(self.config.get("vad_onset_seconds", 0.096)) * SAMPLE_RATE / VAD_FRAME_SAMPLES))
            silence_target = max(1, round(float(self.config.get("phrase_silence_seconds", 0.80)) * SAMPLE_RATE / VAD_FRAME_SAMPLES))
            min_speech = int(float(self.config.get("vad_min_speech_seconds", 0.25)) * SAMPLE_RATE)
            last_interim = time.monotonic()
            while not self.stop_event.is_set():
                raw, _ = stream.read(VAD_FRAME_SAMPLES)
                frame = np.frombuffer(raw, dtype="<i2").copy()
                prob = self.vad.probability(frame)
                if not active:
                    self.pre_roll.append(frame)
                    onset = onset + 1 if prob >= float(self.config.get("vad_start_threshold", 0.50)) else 0
                    if onset >= onset_target:
                        active = True
                        self.phrase_id += 1
                        self.ring.reset()
                        for p in self.pre_roll:
                            self.ring.append(p)
                        if self.typer:
                            self.typer.reset()
                else:
                    self.ring.append(frame)
                    silence = silence + 1 if prob < float(self.config.get("vad_end_threshold", 0.35)) else 0
                    now = time.monotonic()
                    if self.realtime and (now - last_interim) >= float(self.config.get("realtime_interval_seconds", 1.2)):
                        last_interim = now
                        if len(self.ring) >= min_speech:
                            req = self.daemon.worker.submit(self.ring.read(), {"session_id": self.session_id,
                                "phrase_id": self.phrase_id, "final": False}, force=False)
                            if req:
                                res = self.daemon.worker.wait_result(req, 1.0)
                                if res and res.get("text") and res.get("ok", True) and self.typer:
                                    self.typer.update(res["text"], final=False)
                    max_samples = int(float(self.config.get("max_phrase_seconds", 15.0)) * SAMPLE_RATE)
                    if silence >= silence_target or len(self.ring) >= max_samples:
                        active = False
                        onset = silence = 0
                        if len(self.ring) >= min_speech:
                            req = self.daemon.worker.submit(self.ring.read(), {"session_id": self.session_id,
                                "phrase_id": self.phrase_id, "final": True}, force=True)
                            if req:
                                res = self.daemon.worker.wait_result(req, float(self.config.get("finalize_timeout_seconds", 120.0)))
                                if res and res.get("text") and res.get("ok", True):
                                    txt = res["text"].strip()
                                    if self.typer:
                                        self.typer.update(txt, final=True)
                                    self.phrases.append(txt)
                        self.vad.reset()
                        if self.ring.dropped_samples:
                            LOG.warning("Ring overflow dropped %d samples", self.ring.dropped_samples)
        return self._publish(self._finalize_text(" ".join(self.phrases)))

    def run_file(self, path: Path) -> str:
        chunks = decode_file_to_pcm(path, float(self.config.get("file_chunk_seconds", 25.0)))
        texts: list[str] = []
        for i, ch in enumerate(chunks):
            if self.stop_event.is_set():
                break
            req = self.daemon.worker.submit(ch, {"session_id": self.session_id, "phrase_id": i + 1, "final": True}, force=True)
            if not req:
                continue
            res = self.daemon.worker.wait_result(req, float(self.config.get("finalize_timeout_seconds", 120.0)))
            if res and res.get("text") and res.get("ok", True):
                texts.append(res["text"].strip())
        return self._publish(self._finalize_text(" ".join(texts)))


class DuskyDaemon:
    def __init__(self, config_path: Path) -> None:
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        if self.config.get("schema_version") != 2:
            raise RuntimeError("config schema_version must be 2")
        self.worker = WorkerManager(self.config)
        self.state = "idle"
        self._lock = threading.RLock()
        self._session: RecordingSession | None = None
        self._file_session: RecordingSession | None = None
        self._stop = threading.Event()
        self._start_time = time.monotonic()
        rt = os.environ.get("XDG_RUNTIME_DIR")
        if not rt:
            raise RuntimeError("XDG_RUNTIME_DIR unset")
        self.control_path = Path(rt) / "dusky-stt" / "control.sock"
        self._listener = self._bind_socket()

    def _bind_socket(self) -> socket.socket:
        self.control_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.control_path.parent, 0o700)
        self.control_path.unlink(missing_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        old = os.umask(0o177)
        try:
            s.bind(str(self.control_path))
        finally:
            os.umask(old)
        os.chmod(self.control_path, 0o600)
        s.listen(16)
        s.setblocking(False)
        return s

    def status(self) -> JsonObject:
        with self._lock:
            return {"ok": True, "state": self.state, "pid": os.getpid(), "worker_pid": self.worker.pid,
                    "hardware": self.config.get("hardware", "cpu"),
                    "uptime_seconds": round(time.monotonic() - self._start_time, 1),
                    "rss_kib": self._rss(), "cuda_maps": cuda_maps(),
                    "dropped_samples": self._session.ring.dropped_samples if self._session else 0}

    @staticmethod
    def _rss() -> int:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        sel = selectors.DefaultSelector()
        sel.register(self._listener, selectors.EVENT_READ)
        interval = watchdog_interval()
        systemd_notify("READY=1\nSTATUS=Dusky STT: idle")
        nxt = time.monotonic() + interval if interval else 0.0
        try:
            while not self._stop.is_set():
                timeout = max(0.05, min(0.5, nxt - time.monotonic())) if interval else 0.5
                for key, _ in sel.select(timeout=timeout):
                    if key.fileobj is self._listener:
                        try:
                            conn, _ = self._listener.accept()
                        except (BlockingIOError, OSError):
                            continue
                        threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
                if interval and time.monotonic() >= nxt:
                    systemd_notify("WATCHDOG=1")
                    nxt = time.monotonic() + interval
        finally:
            systemd_notify("STOPPING=1")
            sel.close()
            self._listener.close()
            self.worker.stop()
            self.control_path.unlink(missing_ok=True)
        return 0

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", cred)
                if uid != os.getuid():
                    return
                data = conn.recv(MAX_PACKET)
                if not data:
                    return
                req = json.loads(data.decode("utf-8"))
            except (OSError, ValueError):
                return
            cmd = req.get("command")
            resp: JsonObject = {"ok": False, "error": f"unknown command {cmd!r}"}
            with self._lock:
                if cmd == "status":
                    resp = self.status()
                elif cmd in ("start", "toggle"):
                    if self.state == "idle":
                        realtime = req.get("mode", "realtime") != "push"
                        self._session = RecordingSession(self, realtime)
                        threading.Thread(target=self._run_session, args=(self._session, False, None), daemon=True).start()
                        resp = {"ok": True, "state": "recording"}
                    elif cmd == "toggle" and self._session:
                        self._session.stop_event.set()
                        resp = {"ok": True, "state": "finalizing"}
                    else:
                        resp = {"ok": False, "error": "already recording", "state": self.state}
                elif cmd == "stop":
                    if self._session:
                        self._session.stop_event.set()
                        resp = {"ok": True, "state": "finalizing"}
                    else:
                        resp = {"ok": False, "error": "not recording", "state": self.state}
                elif cmd == "file":
                    if self.state == "idle":
                        try:
                            p = Path(str(req.get("path", ""))).expanduser()
                            if not p.is_file():
                                # PrivateTmp=yes gives the daemon a private /tmp:
                                # host /tmp files are invisible by design.
                                resp = {"ok": False, "error": f"file not found (sandbox: place files under $HOME, not /tmp): {p}"}
                            else:
                                self._session = RecordingSession(self, False)
                                threading.Thread(target=self._run_session, args=(self._session, True, p), daemon=True).start()
                                resp = {"ok": True, "state": "transcribing"}
                        except (OSError, ValueError) as exc:
                            resp = {"ok": False, "error": str(exc)}
                    else:
                        resp = {"ok": False, "error": "busy", "state": self.state}
            try:
                conn.sendall(json.dumps(resp).encode())
            except OSError:
                pass

    def _run_session(self, sess: RecordingSession, is_file: bool, path: Path | None) -> None:
        self.state = "transcribing" if is_file else "recording"
        systemd_notify(f"STATUS=Dusky STT: {self.state}")
        try:
            sess.run_file(path) if (is_file and path) else sess.run()
        except Exception as exc:
            LOG.error("Session failed: %s", exc)
        finally:
            with self._lock:
                self.state = "idle"
                self._session = None
            systemd_notify("STATUS=Dusky STT: idle")


def main() -> int:
    global APP_DIR, CONFIG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--check-cpu-isolation", action="store_true")
    args = ap.parse_args()
    CONFIG_PATH = args.config
    APP_DIR = Path(os.environ.get("DUSKY_APP_DIR", args.config.parent if args.config.name == "config.json" else APP_DIR))
    assert_cpu_ort_namespace()
    if args.check_cpu_isolation:
        print(json.dumps({"ok": True, "isolation": "clean", "cuda_maps": cuda_maps()}))
        return 0
    return DuskyDaemon(CONFIG_PATH).run()


if __name__ == "__main__":
    sys.exit(main())
