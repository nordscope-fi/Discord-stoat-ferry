"""Original-behaviour subprocess proof for native startup recovery."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "native_startup"
FAIL_CREATE = FIXTURES / "fail_create"


def _child_environment(renderer: Path) -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(renderer)}
    environment.pop("PYTEST_CURRENT_TEST", None)
    return environment


def _run_harness(renderer: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(FIXTURES / "harness.py"),
            "--storage-dir",
            str(tmp_path),
            "--mode",
            "recover",
        ],
        capture_output=True,
        check=False,
        env=_child_environment(renderer),
        text=True,
        timeout=15,
    )


def test_original_native_child_failure_needs_recovery(tmp_path: Path) -> None:
    """The old launcher exits after a child failure rather than retaining HTTP."""
    result = _run_harness(FAIL_CREATE, tmp_path)

    assert "NATIVE_STARTUP_FAILURE phase=create" in result.stdout
    assert "RECOVERY_OBSERVER_ARMED" in result.stdout
    assert "NATIVE_FAILURE_OBSERVED" in result.stdout
    assert "HTTP_READY status=200" in result.stdout
