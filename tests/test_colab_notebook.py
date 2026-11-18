"""The Colab template is valid, has no outputs or secrets, and uses the public repo URL."""

import json
import re
from pathlib import Path

from market_intel.disclaimer import DISCLAIMER

NOTEBOOK = Path(__file__).resolve().parent.parent / "notebooks" / "colab_template.ipynb"
CLONE_URL = "https://github.com/MichkelAngloJ/market-intel.git"


def load():
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))  # valid JSON or this fails


def source(cell) -> str:
    return "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]


def test_notebook_is_valid_nbformat_4():
    nb = load()
    assert nb["nbformat"] == 4 and nb["cells"]


def test_no_outputs_or_execution_counts():
    for cell in load()["cells"]:
        if cell["cell_type"] == "code":
            assert cell.get("outputs") == []
            assert cell.get("execution_count") is None


def test_clone_url_and_steps():
    text = "\n".join(source(c) for c in load()["cells"])
    assert f"git clone --depth 1 {CLONE_URL}" in text
    assert "!nvidia-smi" in text and "!python -m market_intel.compute_check" in text
    assert "(3, 12)" in text  # Python version guard
    assert "decided in Phase 3" in text
    assert DISCLAIMER in text
    assert "Secrets panel" in text and "This repo is public" in text


def test_no_uncommented_userdata_get():
    for cell in load()["cells"]:
        if cell["cell_type"] != "code":
            continue
        for line in source(cell).splitlines():
            if "userdata" in line:
                assert line.lstrip().startswith("#"), f"uncommented: {line!r}"


def test_no_secret_looking_values():
    text = NOTEBOOK.read_text(encoding="utf-8")
    assert not re.search(r"(postgres|postgresql|mongodb)(\+\w+)?://", text)
    assert not re.search(r"(?i)(api[_-]?key|password|secret)\s*=\s*['\"][^'\"]+['\"]", text)
