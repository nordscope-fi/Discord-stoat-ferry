"""Static and runtime checks for the isolated feedback service image."""

from __future__ import annotations

import base64
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "services" / "feedback" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
UV_REQUIREMENTS = ROOT / "services" / "feedback" / "uv-requirements.txt"


def test_feedback_container_has_a_locked_non_root_runtime() -> None:
    source = DOCKERFILE.read_text()
    uv_requirements = UV_REQUIREMENTS.read_text()

    assert source.count("FROM python:3.11.13-slim-bookworm") == 2
    assert "pip install --require-hashes" in source
    assert "uv-requirements.txt" in source
    assert uv_requirements.startswith("uv==0.12.7 \\\n")
    assert uv_requirements.count("--hash=sha256:") == 3
    assert "uv sync --frozen --no-dev --no-editable --extra feedback-service" in source
    assert "USER 10001:10001" in source
    assert source.count("EXPOSE 8080") == 1
    assert 'VOLUME ["/data"]' in source
    assert "http://127.0.0.1:8080/health" in source
    assert (
        'CMD ["python", "-m", "discord_ferry.feedback_service", "serve", '
        '"--host", "0.0.0.0", "--port", "8080"]'
    ) in source
    assert "ferry-desktop" not in source
    assert "ferry-gui" not in source
    assert "PRIVATE KEY" not in source
    assert ".pem" not in source


def test_feedback_container_context_excludes_private_and_desktop_artifacts() -> None:
    source = DOCKERIGNORE.read_text()

    for pattern in (
        ".git",
        ".venv",
        ".env",
        "*.pem",
        "*.key",
        "tests",
        "docs",
        "release-assets",
    ):
        assert pattern in source


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_feedback_container_runs_read_only_with_only_data_writable(tmp_path: Path) -> None:
    tag = f"discord-ferry-feedback-test:{time.time_ns()}"
    name = f"discord-ferry-feedback-test-{time.time_ns()}"
    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    encoded_keys = [base64.urlsafe_b64encode(bytes([fill]) * 32).decode() for fill in (1, 2, 3)]
    environment = {
        "FERRY_FEEDBACK_REPOSITORY": "nordscope-fi/Discord-stoat-ferry",
        "FERRY_FEEDBACK_GITHUB_APP_ID": "4773301",
        "FERRY_FEEDBACK_GITHUB_INSTALLATION_ID": "157795120",
        "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY": _private_key(),
        "FERRY_FEEDBACK_DATABASE_PATH": "/data/feedback.sqlite3",
        "FERRY_FEEDBACK_CHALLENGE_KEY": encoded_keys[0],
        "FERRY_FEEDBACK_SOURCE_HASH_KEY": encoded_keys[1],
        "FERRY_FEEDBACK_CONTACT_KEY": encoded_keys[2],
        "FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS": "172.16.0.0/12",
    }

    subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", tag, str(ROOT)],
        check=True,
        timeout=300,
    )
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--publish",
        f"127.0.0.1:{port}:8080",
        "--volume",
        f"{data}:/data",
    ]
    for key, value in environment.items():
        command.extend(("--env", f"{key}={value}"))
    command.append(tag)

    subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health",
                    timeout=2,
                ) as response:
                    assert response.status == 200
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        user = subprocess.run(
            ["docker", "exec", name, "id", "-u"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert user.stdout.strip() == "10001"
        assert (data / "feedback.sqlite3").is_file()
        outside = subprocess.run(
            ["docker", "exec", name, "python", "-c", "open('/forbidden', 'w').close()"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert outside.returncode != 0
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            check=False,
            text=True,
        )
