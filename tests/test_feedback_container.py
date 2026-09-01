"""Static and runtime checks for the isolated feedback service image."""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

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
    assert "apt-get install --yes --no-install-recommends curl" in source
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


def _require_docker() -> None:
    if _docker_available():
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail("Docker daemon is unavailable on GitHub Actions")
    pytest.skip("Docker daemon is unavailable")


def test_docker_unavailable_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_docker_available", lambda: False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="Docker daemon is unavailable"):
        _require_docker()


def test_docker_unavailable_fails_on_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_docker_available", lambda: False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(pytest.fail.Exception, match="Docker daemon is unavailable"):
        _require_docker()


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


def test_feedback_container_runs_read_only_with_only_data_writable() -> None:
    _require_docker()
    tag = f"discord-ferry-feedback-test:{time.time_ns()}"
    name = f"discord-ferry-feedback-test-{time.time_ns()}"
    volume = f"{name}-data"
    port = _free_port()
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
    try:
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
            f"{volume}:/data",
        ]
        for key, value in environment.items():
            command.extend(("--env", f"{key}={value}"))
        command.append(tag)

        subprocess.run(
            ["docker", "volume", "create", volume],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
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
                        logs = subprocess.run(
                            ["docker", "logs", name],
                            capture_output=True,
                            check=False,
                            text=True,
                        )
                        pytest.fail(
                            f"feedback container did not become healthy:\n{logs.stdout}"
                            f"{logs.stderr}"
                        )
                    time.sleep(0.2)
            user = subprocess.run(
                ["docker", "exec", name, "id", "-u"],
                check=True,
                capture_output=True,
                text=True,
            )
            assert user.stdout.strip() == "10001"
            subprocess.run(
                ["docker", "exec", name, "curl", "--fail", "http://127.0.0.1:8080/health"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            read_only = subprocess.run(
                ["docker", "inspect", "--format", "{{.HostConfig.ReadonlyRootfs}}", name],
                check=True,
                capture_output=True,
                text=True,
            )
            assert read_only.stdout.strip() == "true"
            database = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path; assert Path('/data/feedback.sqlite3').is_file()",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            assert database.returncode == 0, database.stderr
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
            subprocess.run(
                ["docker", "volume", "rm", "--force", volume],
                capture_output=True,
                check=False,
                text=True,
            )
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", "--no-prune", tag],
            capture_output=True,
            check=False,
            text=True,
        )


def test_feedback_container_cleanup_removes_current_resources_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = "discord-ferry-feedback-test:101"
    name = "discord-ferry-feedback-test-202"
    volume = f"{name}-data"
    timestamps = iter((101, 202))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), dict(kwargs)))
        stdout = ""
        returncode = 0
        if command[-2:] == ["id", "-u"]:
            stdout = "10001\n"
        elif command[:3] == ["docker", "inspect", "--format"]:
            stdout = "true\n"
        elif command[-1] == "open('/forbidden', 'w').close()":
            returncode = 1
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(sys.modules[__name__], "_require_docker", lambda: None)
    monkeypatch.setattr(sys.modules[__name__], "_private_key", lambda: "test-key")
    monkeypatch.setattr(sys.modules[__name__], "_free_port", lambda: 45_678)
    monkeypatch.setattr(time, "time_ns", lambda: next(timestamps))
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: contextlib.nullcontext(SimpleNamespace(status=200)),
    )

    test_feedback_container_runs_read_only_with_only_data_writable()

    commands = [command for command, _kwargs in calls]
    assert commands[-3:] == [
        ["docker", "rm", "--force", name],
        ["docker", "volume", "rm", "--force", volume],
        ["docker", "image", "rm", "--force", "--no-prune", tag],
    ]
    assert [command for command in commands if command[:2] == ["docker", "image"]] == [
        ["docker", "image", "rm", "--force", "--no-prune", tag]
    ]
    assert calls[-1][1]["check"] is False


def test_feedback_container_cleanup_preserves_post_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = "discord-ferry-feedback-test:303"
    name = "discord-ferry-feedback-test-404"
    volume = f"{name}-data"
    timestamps = iter((303, 404))
    calls: list[tuple[list[str], dict[str, object]]] = []
    volume_error = subprocess.CalledProcessError(
        17,
        ["docker", "volume", "create", volume],
    )

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), dict(kwargs)))
        if command[:3] == ["docker", "volume", "create"]:
            raise volume_error
        if command[:3] == ["docker", "image", "rm"]:
            if kwargs.get("check") is not False:
                raise subprocess.CalledProcessError(23, command)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="in use")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(sys.modules[__name__], "_require_docker", lambda: None)
    monkeypatch.setattr(sys.modules[__name__], "_private_key", lambda: "test-key")
    monkeypatch.setattr(sys.modules[__name__], "_free_port", lambda: 45_678)
    monkeypatch.setattr(time, "time_ns", lambda: next(timestamps))
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        test_feedback_container_runs_read_only_with_only_data_writable()

    assert caught.value is volume_error
    assert calls[-1] == (
        ["docker", "image", "rm", "--force", "--no-prune", tag],
        {"capture_output": True, "check": False, "text": True},
    )
