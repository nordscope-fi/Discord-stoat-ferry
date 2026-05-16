"""Contract test: real DCE binary still exposes the flags Ferry depends on.

Uses the canonical `_get_dce_dir()` location so the CI cache key
`dce-${{ runner.os }}-2.47.1` actually populates and reuses the binary
across runs. Without this, every CI run would re-download ~30MB and hit
GitHub's unauthenticated rate limit (60/hr per IP).
"""

from __future__ import annotations

import subprocess
from unittest.mock import sentinel

import pytest

from discord_ferry.exporter.manager import DCE_VERSION, download_dce
from discord_ferry.exporter.runner import _build_dce_command


def _required_flags_from_runner() -> tuple[str, ...]:
    """Render `_build_dce_command` with sentinels and extract the flag tokens.

    Avoids duplicating Ferry's flag list in the test (which would silently
    drift from `_build_dce_command`). Any flag Ferry passes -- present or
    future -- is automatically picked up.
    """

    class _Cfg:
        discord_token = "SENTINEL_TOKEN"
        discord_server_id = "SENTINEL_GUILD"
        export_dir = sentinel.export_dir  # str() call stringifies it

    argv = _build_dce_command(_Cfg(), sentinel.dce_path)
    # argv[0] is the dce path; argv[1] is the subcommand ("exportguild");
    # the rest alternates between flags ("--token", "-g", ...) and values.
    flags: list[str] = [argv[1]]  # subcommand
    flags.extend(tok for tok in argv[2:] if tok.startswith("-"))
    return tuple(flags)


REQUIRED_FLAGS = _required_flags_from_runner()


@pytest.mark.asyncio
async def test_dce_help_lists_all_flags_ferry_uses() -> None:
    """Real DCE --help output still contains every flag _build_dce_command() passes."""
    dce_path = await download_dce(lambda _: None)
    assert dce_path.exists(), "download_dce returned a non-existent path"

    result = subprocess.run(
        [str(dce_path), "exportguild", "--help"],
        capture_output=True,
        text=True,
        timeout=60,  # 60s headroom: cold-start dotnet + ~10s JIT on slow CI runners
    )
    output = result.stdout + result.stderr

    missing = [flag for flag in REQUIRED_FLAGS if flag not in output]
    assert not missing, (
        f"DCE v{DCE_VERSION} dropped these flags Ferry depends on: {missing}. "
        f"Either DCE renamed/removed the flag (update `_build_dce_command`) or "
        f"pin a different DCE_VERSION in `discord_ferry.exporter.manager`."
    )
