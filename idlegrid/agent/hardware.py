"""Detect what this machine can contribute: Apple Silicon (MLX) or NVIDIA (CUDA)."""

import platform
import shutil
import socket
import subprocess


def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _mac_usable_gb(total_gb: float) -> float:
    """How much memory the GPU may actually use. Metal reports a recommended working
    set; fall back to the macOS default split (~2/3 on small machines, ~3/4 on big)."""
    try:
        import mlx.core as mx
        info = mx.device_info() if hasattr(mx, "device_info") else mx.metal.device_info()
        rec = info.get("max_recommended_working_set_size")
        if rec:
            return round(rec / 2**30, 1)
    except Exception:
        pass
    wired = _sh("sysctl", "-n", "iogpu.wired_limit_mb")
    if wired.isdigit() and int(wired) > 0:
        return round(int(wired) / 1024, 1)
    return round(total_gb * (0.67 if total_gb <= 36 else 0.75), 1)


def detect() -> dict:
    name = socket.gethostname().split(".")[0]
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        total = int(_sh("sysctl", "-n", "hw.memsize") or 0) / 2**30
        return {"name": name, "platform": "mac", "chip": _sh("sysctl", "-n", "machdep.cpu.brand_string") or "Apple Silicon",
                "memory_gb": round(total, 1), "usable_mem_gb": _mac_usable_gb(total), "backends": ["mlx"]}
    if shutil.which("nvidia-smi"):
        out = _sh("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits").splitlines()
        if out:
            gpu, mem_mb = [x.strip() for x in out[0].split(",")]
            vram = int(mem_mb) / 1024
            return {"name": name, "platform": platform.system().lower(), "chip": gpu, "memory_gb": round(vram, 1),
                    "usable_mem_gb": round(vram * 0.9, 1), "backends": ["llamacpp"]}
    return {"name": name, "platform": platform.system().lower(), "chip": platform.processor() or "cpu",
            "memory_gb": 0, "usable_mem_gb": 0, "backends": []}


def eligibility(hw: dict, min_usable_gb: float = 1.2) -> tuple[bool, str]:
    if not hw["backends"]:
        return False, "No supported accelerator found (needs an Apple Silicon Mac or an NVIDIA GPU)."
    if hw["usable_mem_gb"] < min_usable_gb:
        return False, f"Only {hw['usable_mem_gb']} GB of GPU memory is usable; at least {min_usable_gb} GB is needed."
    return True, "ok"
