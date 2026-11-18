"""compute_check with and without nvidia-smi (mocked). Never runs the real nvidia-smi."""

import json
import subprocess
from types import SimpleNamespace

from market_intel import compute_check


def fake_run(stdout="", returncode=0, exc=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if exc:
            raise exc
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    run.calls = calls
    return run


def test_no_nvidia_smi():
    info = compute_check.gpu_info(which=lambda _n: None, run=fake_run())
    assert info == {"found": False, "gpus": [], "note": "nvidia-smi not found"}
    full = compute_check.collect(gpu=info)
    assert full["recommendation"] == "CPU only: use Colab for heavy training"
    assert "No NVIDIA GPU found (nvidia-smi not found)" in compute_check.render(full)


def test_gpu_found():
    run = fake_run(stdout="Tesla T4, 15360 MiB, 550.54.15\n")
    info = compute_check.gpu_info(which=lambda _n: "/usr/bin/nvidia-smi", run=run)
    assert info["found"] is True
    assert info["gpus"] == [
        {"name": "Tesla T4", "memory_total": "15360 MiB", "driver": "550.54.15"}
    ]
    cmd, kwargs = run.calls[0]
    assert cmd == compute_check.GPU_QUERY and kwargs["timeout"] == 10
    full = compute_check.collect(gpu=info)
    assert full["recommendation"] == "GPU found: Tesla T4, 15360 MiB"


def test_nvidia_smi_errors_are_handled():
    which = lambda _n: "nvidia-smi"  # noqa: E731
    assert compute_check.gpu_info(which, fake_run(returncode=9))["note"] == "nvidia-smi exit code 9"
    timeout = subprocess.TimeoutExpired("nvidia-smi", 10)
    assert compute_check.gpu_info(which, fake_run(exc=timeout))["note"] == "nvidia-smi timed out"
    assert "failed" in compute_check.gpu_info(which, fake_run(exc=OSError()))["note"]
    assert compute_check.gpu_info(which, fake_run(stdout=""))["found"] is False


def test_collect_fields_and_json(monkeypatch, capsys):
    monkeypatch.setattr(
        compute_check, "gpu_info", lambda: {"found": False, "gpus": [], "note": "x"}
    )
    assert compute_check.main(["--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    for key in ("os", "python", "cpu_logical_cores", "ram_total_gb", "disk_free_gb", "gpu"):
        assert key in info
    assert info["cpu_logical_cores"] >= 1 and info["ram_total_gb"] > 0
