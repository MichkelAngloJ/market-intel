"""What can this machine compute? ``uv run python -m market_intel.compute_check`` (``--json``).

Shows OS, Python, CPU cores, RAM, free disk on the repo's drive and any NVIDIA GPU, plus a
recommendation. The GPU is found with ``nvidia-smi`` (no PyTorch yet; the PyTorch CUDA check
comes in Phase 3). Needs no settings and no .env, so it also runs on Colab as-is.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
NVIDIA_SMI_TIMEOUT_S = 10
GPU_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,memory.total,driver_version",
    "--format=csv,noheader",
]


def gpu_info(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """{'found': bool, 'gpus': [...], 'note': str}. Never raises."""
    if which("nvidia-smi") is None:
        return {"found": False, "gpus": [], "note": "nvidia-smi not found"}
    try:
        result = run(
            GPU_QUERY, capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return {"found": False, "gpus": [], "note": "nvidia-smi timed out"}
    except OSError as exc:
        return {"found": False, "gpus": [], "note": f"nvidia-smi failed: {type(exc).__name__}"}
    if result.returncode != 0:
        return {"found": False, "gpus": [], "note": f"nvidia-smi exit code {result.returncode}"}
    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "memory_total": parts[1], "driver": parts[2]})
    if not gpus:
        return {"found": False, "gpus": [], "note": "nvidia-smi listed no GPU"}
    return {"found": True, "gpus": gpus, "note": ""}


def collect(gpu: dict[str, Any] | None = None) -> dict[str, Any]:
    mem = psutil.virtual_memory()
    disk = shutil.disk_usage(REPO_ROOT)
    gpu = gpu if gpu is not None else gpu_info()
    if gpu["found"]:
        first = gpu["gpus"][0]
        recommendation = f"GPU found: {first['name']}, {first['memory_total']}"
    else:
        recommendation = "CPU only: use Colab for heavy training"
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "ram_available_gb": round(mem.available / 1024**3, 1),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "gpu": gpu,
        "recommendation": recommendation,
    }


def render(info: dict[str, Any]) -> str:
    gpu = info["gpu"]
    if gpu["found"]:
        gpu_lines = [f"{g['name']}, {g['memory_total']}, driver {g['driver']}" for g in gpu["gpus"]]
    else:
        gpu_lines = [f"No NVIDIA GPU found ({gpu['note']})"]
    lines = [
        f"OS            : {info['os']}",
        f"Python        : {info['python']}",
        f"CPU cores     : {info['cpu_physical_cores']} physical, "
        f"{info['cpu_logical_cores']} logical",
        f"RAM           : {info['ram_available_gb']} GB available of {info['ram_total_gb']} GB",
        f"Disk free     : {info['disk_free_gb']} GB (repo drive)",
        *[f"GPU           : {line}" for line in gpu_lines],
        "",
        f"Recommendation: {info['recommendation']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m market_intel.compute_check")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    info = collect()
    print(json.dumps(info, indent=2) if args.json else render(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
