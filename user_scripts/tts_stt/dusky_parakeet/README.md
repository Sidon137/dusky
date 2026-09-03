# Dusky STT (bleeding-edge Arch, hardware-agnostic)

Local speech-to-text for Arch Linux rolling on Wayland. No usernames or machines hardcoded — everything resolves from `$HOME`, `%h`, `$USER`, `$XDG_RUNTIME_DIR`.

- **CPU daemon** (`.venv-main`): capture, Silero VAD v6.2.1, live typing, control plane. Never maps CUDA.
- **On-demand worker** (`.venv-worker`): Parakeet TDT 0.6B via ONNX Runtime 1.27.0. EP follows `--hardware`: `CUDAExecutionProvider` (NVIDIA), opportunistic `MIGraphX/ROCM` else CPU (AMD), `CPUExecutionProvider` (CPU). Exits on idle so NVIDIA dGPUs can reach D3cold.
- **IPC**: sealed memfds (`MFD_NOEXEC_SEAL` + `F_SEAL_EXEC=0x20`) over `SOCK_SEQPACKET` + `SCM_RIGHTS`. Large replies return via a second memfd, never truncated.

Target: Arch rolling, kernel 6.10+ (7.x tested), CPython 3.14.6+ GIL, `uv`, NVIDIA driver 580+ (for nvidia mode).

## Install (you run this)

```bash
chmod +x dusky_installer.py dusky_main.py dusky_worker.py dusky_trigger.py dusky_verify.sh
./dusky_installer.py --hardware auto --model nemo-parakeet-tdt-0.6b-v2 --quantization int8
# explicit: --hardware cpu | nvidia | amd
# NVIDIA example: ./dusky_installer.py --hardware nvidia --gpu-mem-limit-mb 2867
# skip pacman if already present: --skip-pacman
```

Auto-detect order: `nvidia` (nvidia-smi + driver 580+) → `amd` (rocm-smi / /dev/kfd / AMD VGA) → `cpu`. AMD without a ROCm stack uses CPU reliably; install system ROCm + MIGraphX separately for experimental GPU EPs.

## Use (your existing keybind keeps working; bare `dusky_trigger` toggles)

```bash
dusky_trigger                 # toggle realtime (bind this to hotkey)
dusky_trigger --status
dusky_trigger --start --realtime | dusky_trigger --stop
dusky_trigger --start --push   | dusky_trigger --stop
dusky_trigger --file ~/audio.m4a
dusky_trigger --logs | dusky_trigger --restart | dusky_trigger --kill
```

## Verify

```bash
dusky_verify static
dusky_verify live
dusky_verify d3     # nvidia only; skipped otherwise
dusky_verify all
```

D3cold needs `NVreg_DynamicPowerManagement=0x02` and no other GPU clients; the check reads sysfs passively (running `nvidia-smi` wakes the GPU).

## Uninstall

```bash
./dusky_installer.py --uninstall
```
