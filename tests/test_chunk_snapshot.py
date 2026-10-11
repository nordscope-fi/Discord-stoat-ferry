from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Git mode fixtures run in the Linux CI jobs"
)

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def run_fixture(tmp_path: Path, case: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [
            NODE,
            "tests/fixtures/chunk_snapshot_runner.mjs",
            case,
            str(tmp_path),
            *arguments,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_capture_preserves_bytes_modes_new_files_and_deletions(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-content")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["round_trip"] is True
    assert report["modes"] == ["100644", "100755"]
    assert report["new_file"] == {"before": False, "after": True}
    assert report["deleted_file"] == {"before": True, "after": False}
    assert report["binary_sha256"] == report["saved_binary_sha256"]
    assert report["starting_tree"] == report["expected_starting_tree"]
    assert report["ending_tree"] == report["expected_ending_tree"]


def test_capture_rejects_unreadable_content_without_success_record(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-unreadable")
    if result.returncode == 0 and json.loads(result.stdout).get("permission_guard_unsupported"):
        pytest.skip("the current privileged host can read mode-000 files")
    assert result.returncode == 1
    assert "chunk 1" in result.stderr
    assert "unreadable.bin" in result.stderr
    assert not (tmp_path / "repo/docs/plans/.review/chunk-snapshots/tasks/1002/patch.json").exists()


def test_capture_rejects_dangling_candidate_link_before_read(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-dangling-link")
    assert result.returncode == 1
    assert "rejected before read: dangling.txt" in result.stderr
    assert not (tmp_path / "repo/docs/plans/.review/chunk-snapshots/tasks/1002/patch.json").exists()


def test_capture_rejects_escaping_evidence_root_before_write(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "reject-escaping-evidence-root")
    assert result.returncode == 1
    assert "evidence root must be a clean relative path" in result.stderr
    assert not (tmp_path / "outside-evidence").exists()


def test_capture_rejects_dangling_evidence_directory_before_write(
    tmp_path: Path,
) -> None:
    result = run_fixture(tmp_path, "reject-dangling-evidence-directory")
    assert result.returncode == 1
    assert "evidence directory is unsafe" in result.stderr
    assert not (tmp_path / "missing-evidence-target").exists()


def test_baseline_replays_prior_task_record(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "prior-task-baseline")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["prior_task_digests"] == [report["expected_prior_digest"]]
    assert report["baseline_sha256"] == report["current_sha256"]


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("missing", "task-patch file does not exist"),
        ("damaged", "task-patch record is invalid"),
        ("current-mismatch", "baseline path does not match prior tasks"),
    ],
)
def test_baseline_rejects_invalid_prior_task_record(
    tmp_path: Path, variant: str, message: str
) -> None:
    result = run_fixture(tmp_path, "prior-task-baseline", variant)
    assert result.returncode == 1
    assert message in result.stderr


def test_baseline_rejects_reordered_prior_task_records(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "prior-task-reordered")
    assert result.returncode == 1
    assert "prior task records are out of order" in result.stderr
