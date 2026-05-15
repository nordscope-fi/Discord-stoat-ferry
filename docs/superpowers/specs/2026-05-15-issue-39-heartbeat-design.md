# Design: silence-breaker heartbeat during DCE export (issue #39)

**Date:** 2026-05-15
**Issue:** [#39](https://github.com/nordscope-fi/discord-stoat-ferry/issues/39)
**Ships as:** Single PR after #23 v2.2.0 lands
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

After #23's parser rewrite (v2.2.0), Ferry surfaces all DCE output to the GUI, including headline lines (`Fetching channels...`, `Fetched N channel(s).`, etc.). This breaks up most silence — but there can still be 5–15 minute silences between phase markers on large guilds (during channel enumeration via Discord REST pagination, where Spectre.Console's status ticker is suppressed in piped mode).

Add a periodic "still working" heartbeat emit while DCE is alive but no meaningful output has arrived for a threshold duration.

## Architecture

A new asyncio task in `runner.py`, launched alongside `_read_stderr`, that monitors a shared "last activity" timestamp and emits heartbeat MigrationEvents when silence exceeds a threshold.

### Adaptive backoff schedule

Per brainstorming decision, intervals double on each consecutive heartbeat (capped at 20 min):

```
First fire:  60s after last activity
Second fire: 120s after first heartbeat fired (with no real activity in between)
Third fire:  240s after second heartbeat
Fourth fire: 480s after third heartbeat
Fifth+ fire: 1200s (capped at 20 min)

Any "real activity" RESETS the schedule back to the 60s initial state.
```

### What counts as "real activity"

For v2.2.0+ (when #23's parser routes all kinds to GUI):

| Parsed kind | Resets heartbeat? | Why |
|-------------|-------------------|-----|
| `per_channel` | YES | Clear progress signal |
| `phase` | YES | Phase transition; user expects pause until next |
| `success` | YES (and ends heartbeat — process about to exit) | Completion |
| `raw` | YES | Unrecognized output is still output |
| `banner` | NO | Initial chrome; not progress |
| `status_dot` | NO | Spectre's silence placeholder |

### Implementation sketch

In `src/discord_ferry/exporter/runner.py`:

```python
import time
import contextlib

async def run_dce_export(config, dce_path, on_event):
    # ... existing setup ...

    process = await asyncio.create_subprocess_exec(...)

    # Mutable container shared with reader and heartbeat tasks
    last_activity = [time.monotonic()]
    process_start = time.monotonic()

    def _record_activity() -> None:
        last_activity[0] = time.monotonic()

    async def _heartbeat() -> None:
        interval = 60.0
        max_interval = 1200.0
        last_heartbeat_fire_at = process_start
        while process.returncode is None:
            await asyncio.sleep(10)
            now = time.monotonic()
            # If activity arrived since last heartbeat fire, reset interval
            if last_activity[0] > last_heartbeat_fire_at:
                interval = 60.0
            silence = now - last_activity[0]
            if silence >= interval:
                elapsed_min = int((now - process_start) / 60)
                silence_sec = int(silence)
                on_event(MigrationEvent(
                    phase="export",
                    status="progress",
                    message=(
                        f"Still working - DCE has been running for {elapsed_min} min, "
                        f"no new output for {silence_sec}s..."
                    ),
                ))
                last_heartbeat_fire_at = now
                interval = min(interval * 2, max_interval)

    stderr_task = asyncio.create_task(_read_stderr())
    heartbeat_task = asyncio.create_task(_heartbeat())

    # ... existing stdout loop ...
    async for raw_line in process.stdout:
        # ... existing decode/cancel-check ...
        parsed = parse_dce_line(line)
        if parsed.kind in {"per_channel", "phase", "success", "raw"}:
            _record_activity()
        _emit_for_parsed(parsed, on_event)

    # ... existing cleanup (try/finally drains stderr_task and heartbeat_task) ...
```

Subtle design: the heartbeat's `interval` doesn't reset just because `last_activity` advances — it resets when the heartbeat task itself sees fresh activity since its last fire. This means: if real activity arrives, the silence calculation drops below `interval` so heartbeat doesn't fire; AND on next loop iteration, the interval resets to 60s. Both effects together produce correct backoff semantics: continuous silence backs off; any activity returns to baseline.

## Components

| Component | Responsibility |
|-----------|----------------|
| `runner._heartbeat` (NEW, async function) | Adaptive-backoff heartbeat task |
| `runner._record_activity` (NEW, sync helper) | Updates `last_activity[0]` |
| `runner.run_dce_export` (UPDATED) | Spawns heartbeat task; calls `_record_activity` on relevant parses; cleans up heartbeat task in finally |
| `tests/test_exporter_runner.py` (UPDATED) | New tests exercising heartbeat firing under simulated silence |

## Data flow

```
DCE stdout line arrives
    |
parse_dce_line(line) -> ParsedDceLine
    |
if kind in {per_channel, phase, success, raw}:
    last_activity[0] = now()  <- updates shared state
    |
_emit_for_parsed(parsed, on_event)  <- still emits to GUI

Concurrently (every 10s tick):
    heartbeat task wakes
    |
    if last_activity > last_heartbeat_fire: interval = 60s (reset backoff)
    if (now - last_activity) >= interval:
        emit heartbeat MigrationEvent
        last_heartbeat_fire = now
        interval *= 2 (cap 1200s)
```

## Error handling

- Heartbeat task wraps its main loop in `try/except asyncio.CancelledError: return` to handle cancellation cleanly.
- Heartbeat task does NOT raise on `on_event` failure — emits are best-effort.
- On cancel via cancel_event, both stderr_task and heartbeat_task are cancelled and awaited in finally.

## Testing

Unit tests in `tests/test_exporter_runner.py` (using mock-clock helpers):

- `test_heartbeat_fires_after_60s_of_silence`: feed a stdout iter that yields nothing for 70 simulated seconds; assert at least one "Still working" event in events list.
- `test_heartbeat_does_not_fire_when_activity_present`: feed lines every 10 simulated seconds for 90s; assert no heartbeat events emitted.
- `test_heartbeat_backoff_doubles_on_consecutive_fires`: pure silence for 5 simulated minutes; assert heartbeats fire at approximately 60s, 180s, 420s, 900s elapsed (60+120, 60+120+240, ...).
- `test_heartbeat_resets_to_60s_on_real_activity`: silence 90s (heartbeat fires at 60s, interval bumps to 120s); then activity at 90s; then silence again. Next heartbeat should fire 60s after the activity, not 120s.
- `test_heartbeat_task_cancelled_on_cancel_event`: trigger cancel_event; assert `heartbeat_task.done()` after `run_dce_export` returns.

Test infrastructure: a `_async_advance_clock` helper that advances a fake monotonic clock and yields control to allow heartbeat task to wake. Mock `asyncio.sleep` to use the fake clock.

Manual smoke test:
- Run real export against a large server (or simulate via slow DCE mock).
- Confirm heartbeat appears at expected intervals in GUI log panel.

## Phasing

Single PR. Lands AFTER #23 v2.2.0 (which provides the new `dce_output` module structure + the routing logic that determines which parsed kinds count as "activity").

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Parser fix makes heartbeat unnecessary" | PARTIALLY FALSIFIED | Phase markers still leave 5–15 min gaps on large guilds during enumeration |
| "Adaptive backoff is right" | VERIFIED via brainstorming | Avoids log spam on long-running exports |
| "60s initial is right starting point" | VERIFIED via brainstorming | Acknowledges user without being noisy |
| "Heartbeat could be implemented at engine layer" | FALSIFIED | Engine is too far up; runner has direct subprocess context |
| "Mock-clock testing is the right approach" | VERIFIED | Real-time tests would be 60+ seconds slow per case; mock clock is instant |

**Foundational?** No — UX enhancement. But once it ships it becomes part of the user-facing UX contract.

## Risks

| Risk | Mitigation |
|------|------------|
| Heartbeat fires during legitimate brief silence (network blip), confuses user | 60s initial is long enough that brief blips don't trigger; adaptive backoff prevents repeated false alarms |
| `time.monotonic` mocking in tests is fragile | Use a single helper function `_async_advance_clock`; document mocking pattern in test file |
| Heartbeat task leaks on exception | Same `try/finally` pattern as `_read_stderr` (covered by #23's stderr drain fix) |
| Heartbeat fires after process completed but before stdout fully drained | Check `process.returncode is None` at start of each loop iteration; once set, exit |
| 1200s (20 min) cap may be too long if DCE is genuinely wedged for hours | User can always cancel; long silences after backoff exhausted indicate real wedge requiring intervention; emitting forever doesn't help |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Tunable interval via Ferry config | Premature; ship default first | — | If users complain about cadence |
| Show "current Discord rate-limit window" in heartbeat message | Requires Discord API integration in runner; out of scope | — | Never likely |
| Diagnose button in GUI to dump current DCE state | Different UX paradigm; might supplement heartbeat | (will file as enhancement) | If users still report "stuck" perception with heartbeat enabled |
| Heartbeat for the upload phase (after export) | Different code path; scope creep | (will file as enhancement) | After this lands and proves valuable |

## Open questions (for implementation, not blocking spec)

- Heartbeat message phrasing: my draft says "Still working - DCE has been running for 5 min, no new output for 65s..." — alternative: "DCE is taking a while: 5 min elapsed, last output 65s ago. Large guilds can take 30+ minutes." Decide during writing-plans based on tone.
- Should heartbeat distinguish phase ("Still fetching channels - Discord REST pagination can be slow on large guilds") vs generic? Requires tracking current phase from headline parses. Probably yes; cleaner UX.
- Whether to expose heartbeat events with a distinct status (e.g., `status="heartbeat"`) so the GUI can render them differently from regular progress events. Probably yes; allows GUI to italicize or color them subtly.

## Cross-references

- Issue #23 v2.2.0 — provides the parser + routing infrastructure this depends on
- Issue #34 — independent
- Issue #35 — captured fixtures could include "long silence" scenario for replay testing of heartbeat
