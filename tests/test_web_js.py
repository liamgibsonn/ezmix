"""Runs the JavaScript tests in tests/js with Node (skipped if Node isn't installed)."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_javascript_tests_pass():
    files = sorted(str(p) for p in JS_TESTS.glob("test_*.mjs"))
    result = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
