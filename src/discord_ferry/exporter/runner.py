"""Async subprocess execution for DiscordChatExporter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.http import new_session, proxy_hint, resolve_proxy_or_raise, tls_hint
from discord_ferry.errors import DiscordAuthError, ExportError
from discord_ferry.exporter.dce_output import (
    Banner,
    ParsedDceLine,
    PerChannel,
    Phase,
    Raw,
    StatusDot,
    Success,
    parse_dce_line,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback

logger = logging.getLogger(__name__)

_DISK_WARN_BYTES = 5_000_000_000  # 5 GB

# Windows console + signal flags. On non-Windows these are 0 (no-op).
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _build_dce_command(config: FerryConfig, dce_path: Path) -> list[str]:
    """Build the DCE CLI command list."""
    return [
        str(dce_path),
        "exportguild",
        "--token",
        config.discord_token or "",
        "-g",
        config.discord_server_id or "",
        "--media",
        "--reuse-media",
        "--markdown",
        "false",
        "--format",
        "Json",
        "--include-threads",
        "All",
        "--output",
        str(config.export_dir),
    ]


def _check_disk_space(export_dir: Path, on_event: EventCallback) -> None:
    """Emit a warning event if disk space is low."""
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(export_dir)
        if usage.free < _DISK_WARN_BYTES:
            free_gb = usage.free / 1_000_000_000
            on_event(
                MigrationEvent(
                    phase="export",
                    status="warning",
                    message=(
                        f"Low disk space ({free_gb:.1f} GB free). "
                        f"Large servers may need 5-10 GB for exports."
                    ),
                )
            )
    except OSError:
        pass  # Can't check disk space -- not critical


@dataclass
class _RunState:
    """Per-export mutable state for tracking overall progress.

    `total_channels` is set once when the `Exporting N channel(s)...` headline
    arrives; subsequent headers are ignored to defend against DCE retries
    re-emitting the header.

    `channels_completed` is a SET (not a counter) for structural defense
    against DCE's per-channel retry behavior: a channel that hits 100%, fails
    transiently, retries from 25%, and hits 100% again would otherwise
    double-count and overshoot total_channels.
    """

    total_channels: int | None = None
    channels_completed: set[str] = field(default_factory=set)


def _emit_for_parsed(
    parsed: ParsedDceLine,
    on_event: EventCallback,
    state: _RunState,
) -> None:
    """Translate a ParsedDceLine into one or more MigrationEvents.

    PerChannel:
      - Updates state on pct==100 (adds channel to channels_completed set).
      - Emits with current=len(channels_completed), total=total_channels.
      - Label format: `Finished <ch>` for 100%, `<ch> (<pct>%)` otherwise.

    Phase(exporting_header):
      - Sets state.total_channels (set-once); emits a progress event.

    Phase(other):
      - Emits the headline message verbatim.

    Success:
      - If count < state.total_channels, emits a warning about silent failures
        BEFORE emitting the success message itself.

    Banner / StatusDot / Raw:
      - Emit prefixed with `[dce] ` to make their provenance clear in the log.
    """
    match parsed:
        case PerChannel(channel=ch, pct=p):
            if p == 100:
                state.channels_completed.add(ch)
            current = len(state.channels_completed)
            total = state.total_channels or 0
            label = f"Finished {ch}" if p == 100 else f"{ch} ({p}%)"
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message=label,
                    channel_name=ch,
                    current=current,
                    total=total,
                )
            )
        case Phase(kind="exporting_header", count=n, message=msg):
            if state.total_channels is None and n is not None:
                state.total_channels = n
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message=msg,
                    current=len(state.channels_completed),
                    total=state.total_channels or 0,
                )
            )
        case Phase(message=msg):
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message=msg,
                )
            )
        case Success(count=s, message=msg):
            if state.total_channels is not None and s < state.total_channels:
                missing = state.total_channels - s
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="warning",
                        message=(
                            f"DCE reports {s} of {state.total_channels} channels "
                            f"exported successfully. {missing} channel(s) appear "
                            "to have failed silently."
                        ),
                    )
                )
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message=msg,
                )
            )
        case Banner(message=msg) | StatusDot(message=msg) | Raw(message=msg):
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message=f"[dce] {msg}",
                )
            )
        case _:
            # Future variants (Error, etc.) -- emit as warning so never silent.
            on_event(
                MigrationEvent(
                    phase="export",
                    status="warning",
                    message=f"[dce] {parsed.message}",
                )
            )


async def _heartbeat(
    process: asyncio.subprocess.Process,
    on_event: EventCallback,
    process_start: float,
    get_last_activity: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    initial_interval: float = 60.0,
    max_interval: float = 300.0,
) -> None:
    """Emit `status="heartbeat"` events during prolonged DCE silence.

    Adaptive backoff: first fires at `initial_interval` (60s) seconds of silence,
    then doubles after each consecutive fire, capped at `max_interval` (300s).
    Any "real activity" (detected by the caller via `get_last_activity`) resets
    the schedule back to the baseline interval.

    The `sleep` and `monotonic` callables are injectable so tests can drive a
    fake clock without monkey-patching the global `asyncio.sleep` (which would
    break the stdout/stderr `async for` loops, `process.wait()`, and aiohttp).
    """
    interval = initial_interval
    last_fire_at = process_start
    try:
        while process.returncode is None:
            now = monotonic()
            silence = now - get_last_activity()
            # Activity since the last heartbeat fire -> reset to baseline.
            if get_last_activity() > last_fire_at:
                interval = initial_interval
            if silence >= interval:
                elapsed_min = int((now - process_start) / 60)
                silence_sec = int(silence)
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="heartbeat",
                        message=(
                            f"Still working - DCE has been running for {elapsed_min} min, "
                            f"no new output for {silence_sec}s..."
                        ),
                    )
                )
                last_fire_at = now
                interval = min(interval * 2, max_interval)
                # Sleep at least a short tick before the next check after firing.
                await sleep(min(10.0, interval))
                continue
            # Adaptive: wake up at most when the next fire could occur, but no
            # less than 1s and no more than 10s, so cancellation stays snappy.
            remaining = max(1.0, min(10.0, interval - silence))
            await sleep(remaining)
    except asyncio.CancelledError:
        return


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Platform-aware subprocess termination.

    POSIX: SIGTERM (process.terminate()) -- DCE has a chance to flush partial
    JSON before exit.

    Windows: CTRL_BREAK_EVENT to the child's process group (safe because we
    spawned with CREATE_NEW_PROCESS_GROUP -- the signal targets only the child,
    not Ferry). DCE's .NET CancelKeyPress handler runs a graceful shutdown.
    Falls back to hard kill after 3s if the child does not exit.
    """
    if sys.platform == "win32":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (ValueError, OSError):
            process.terminate()  # fallback: hard kill
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()
    else:
        process.terminate()
        await process.wait()


async def _drain_overlong_line(
    reader: asyncio.StreamReader, cancel_event: asyncio.Event | None = None
) -> int:
    r"""Consume bytes from `reader` until we reach `\n` (or EOF).

    Used after `readuntil(b"\n")` raised LimitOverrunError to ensure the
    remainder of the over-long line does not end up returned by the next
    `readuntil` call as if it were its own line. Returns total bytes consumed.

    Loops until either:
      - `readuntil` finds the next `\n` (returns tail, total + len(tail))
      - EOF is hit (IncompleteReadError, total + len(exc.partial))
      - Another LimitOverrunError fires (consume that 64KiB chunk and keep going)
      - `cancel_event` is set (stop draining promptly so cancellation isn't
        deferred until a multi-MB line ends)
    """
    total = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return total
        try:
            tail = await reader.readuntil(b"\n")
            total += len(tail)
            return total
        except asyncio.LimitOverrunError as exc:
            chunk = await reader.readexactly(exc.consumed)
            total += len(chunk)
        except asyncio.IncompleteReadError as exc:
            total += len(exc.partial)
            return total


async def validate_discord_token(token: str) -> None:
    """Validate a Discord user token via the /users/@me endpoint.

    Raises:
        DiscordAuthError: If the token is invalid (401), API returns unexpected
            status, or the network is unreachable.
    """
    # Hoisted out of the try on purpose. As an inline literal there was no name
    # the handler could pass as `target`, and proxy_hint takes no default for it.
    url = "https://discord.com/api/v10/users/@me"
    try:
        async with (
            new_session() as session,
            session.get(url, headers={"Authorization": token}) as resp,
        ):
            if resp.status == 401:
                raise DiscordAuthError("Invalid Discord token. Check that you copied it correctly.")
            if resp.status != 200:
                raise DiscordAuthError(f"Discord API returned unexpected status {resp.status}")
    except aiohttp.ClientError as exc:
        # Proxy first, never both, no gate: this handler has no retry.
        hint = proxy_hint(exc, target=url) or tls_hint(exc) or ""
        raise DiscordAuthError(f"Cannot reach Discord API: {exc}{hint}") from exc


async def run_dce_export(
    config: FerryConfig,
    dce_path: Path,
    on_event: EventCallback,
) -> Path:
    """Run DCE as an async subprocess and stream progress.

    Args:
        config: Ferry configuration with discord_token and discord_server_id.
        dce_path: Path to the DCE executable.
        on_event: Callback for progress events.

    Returns:
        Path to the export directory containing JSON files.

    Raises:
        ExportError: If DCE exits with a non-zero code.
        asyncio.CancelledError: If cancelled via config.cancel_event.
    """
    _check_disk_space(config.export_dir, on_event)

    cmd = _build_dce_command(config, dce_path)
    config.export_dir.mkdir(parents=True, exist_ok=True)

    child_env = dict(os.environ)  # full copy: dropping SYSTEMROOT or PATH breaks .NET on Windows
    # The RAISING sibling, with a boundary here. resolve_proxy would swallow and
    # return None, which is indistinguishable from "this machine has no proxy",
    # and there would be nothing to tell the user. The variable below is an
    # optimisation, so a failure to resolve it must never abort an export, but it
    # must not pass silently either: a user behind a proxy whose export runs
    # without one deserves to know why. Issue #148.
    try:
        choice = resolve_proxy_or_raise("https://discord.com/")
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the proxy configuration for the export.", exc_info=True)
        on_event(
            MigrationEvent(
                phase="export",
                status="warning",
                message=(
                    "Could not read the system proxy configuration. The export will run without it."
                ),
            )
        )
        choice = None
    if choice is not None and choice.source == "os" and "HTTPS_PROXY" not in child_env:
        # Only when the OS supplied it. If the user set the variable themselves,
        # DCE already inherits it, and overwriting a deliberate setting is a
        # surprise rather than a feature.
        child_env["HTTPS_PROXY"] = str(choice.url)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        env=child_env,
    )

    process_start = time.monotonic()
    last_activity = process_start

    def _record_activity() -> None:
        nonlocal last_activity
        last_activity = time.monotonic()  # noqa: F821 -- nonlocal is intentional

    def _get_last_activity() -> float:
        return last_activity

    # DCE prints nothing while it enumerates the guild's channels — on large
    # servers that pre-output phase can run several minutes and previously
    # left the GUI looking frozen on the last emitted status.
    on_event(
        MigrationEvent(
            phase="export",
            status="progress",
            message=(
                "DiscordChatExporter started — enumerating channels "
                "(large servers may take several minutes before per-channel progress appears)..."
            ),
        )
    )

    state = _RunState()
    stderr_lines: list[str] = []

    assert process.stdout is not None
    assert process.stderr is not None

    async def _read_stderr() -> None:
        assert process.stderr is not None
        # Mirror the stdout loop: an explicit readuntil loop so a >64 KiB stderr
        # line is drained (not crashed via `async for`'s readline → ValueError).
        while True:
            try:
                raw_line = await process.stderr.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                raw_line = exc.partial
                if not raw_line:
                    break
            except asyncio.LimitOverrunError:
                consumed = await _drain_overlong_line(process.stderr)
                stderr_lines.append(f"<truncated {consumed} bytes; stderr line exceeded 64 KiB>")
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                stderr_lines.append(line)

    stderr_task = asyncio.create_task(_read_stderr())
    heartbeat_task = asyncio.create_task(
        _heartbeat(process, on_event, process_start, _get_last_activity)
    )

    try:
        while True:
            # Check cancel at the top so a cancel during a long drain (below) or a
            # huge blocking read isn't deferred until the next full line.
            if config.cancel_event and config.cancel_event.is_set():
                await _terminate_process(process)
                raise asyncio.CancelledError("Export cancelled by user")

            try:
                raw_line = await process.stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                raw_line = exc.partial
                if not raw_line:
                    break  # clean EOF
            except asyncio.LimitOverrunError:
                # Line longer than 64 KiB -- drain to next \n entirely so the
                # remainder does not get parsed as a separate "line".
                consumed = await _drain_overlong_line(process.stdout, config.cancel_event)
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="progress",
                        message=(f"[dce] <truncated {consumed} bytes; line exceeded 64 KiB>"),
                    )
                )
                _record_activity()  # real output flowed — don't let the heartbeat cry "silent"
                continue

            if config.cancel_event and config.cancel_event.is_set():
                await _terminate_process(process)
                raise asyncio.CancelledError("Export cancelled by user")

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            logger.debug("DCE: %s", line)
            parsed = parse_dce_line(line)
            _emit_for_parsed(parsed, on_event, state)
            if isinstance(parsed, (PerChannel, Phase, Success, Raw, Banner, StatusDot)):
                _record_activity()

        await process.wait()

    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        # IMPORTANT: cancel heartbeat_task FIRST so it cannot fire a false
        # "Still working" event after stdout drains but before process.wait()
        # records returncode. Then drain stderr.
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    if process.returncode != 0:
        last_err = stderr_lines[-1] if stderr_lines else "Unknown error"
        raise ExportError(f"DCE export failed (exit code {process.returncode}): {last_err}")

    return config.export_dir
