"""Every Python source file the app ships must be visible to git. A gitignored module works locally but is missing from a
clean checkout, so the deployed API crashes on import (the 25 Nov 2026 outage: app/services/var was caught by a `var/` rule)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def test_no_app_source_file_is_gitignored():
    if shutil.which("git") is None or subprocess.run(["git", "rev-parse"], cwd=BACKEND, capture_output=True).returncode != 0:
        pytest.skip("not inside a git checkout")
    sources = [str(p.relative_to(BACKEND)) for p in (BACKEND / "app").rglob("*.py") if "__pycache__" not in p.parts]
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=BACKEND, input="\n".join(sources), capture_output=True, text=True)
    ignored = [line for line in result.stdout.splitlines() if line]
    assert ignored == [], f"gitignored app source (would be missing from a clean deploy): {ignored[:10]}"
