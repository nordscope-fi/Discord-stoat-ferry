# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Documentation

- README and docs landing page gained a "Tunable performance" reliability bullet pointing at
  the v2.7.0 flags (concurrency, reaction mode, thread filtering, checkpointing). Docs-only;
  no version bump.

## [2.7.0] - 2026-07-06

### Added

- **The seven internal settings are now user-configurable (closes #99).** `ferry migrate` gains
  `--reaction-mode [text|native|skip]`, `--min-thread-messages` (≥0), `--checkpoint-interval`
  (≥1), `--max-concurrent-channels` (≥1), `--max-concurrent-requests` (≥1), `--skip-avatars`,
  and `--validate-after` — wired through `_common_options`/`_build_config` with parse-time
  `Choice`/`IntRange` validation and unchanged defaults (text/0/50/3/5/off/off). The GUI's
  Advanced Options panel gains the same seven controls under Speed / Content / Safety group
  labels, persisted like the existing controls and normalised by a new
  `_coerce_advanced_settings` helper (clamps stale/out-of-range disk-backed storage values,
  handles `ui.number`'s float-or-None, falls back to `text` on unknown reaction modes).
  Migrate's `--max-concurrent-requests` is unrelated to rollback's same-named
  delete-concurrency flag.
- **Official-service concurrency warning.** Raising either concurrency value above its default
  while targeting `api.stoat.chat` prints a CLI warning / shows a GUI notify (informational,
  never blocks): the official rate limits make higher concurrency slower, not faster.

### Fixed

- **`max_concurrent_channels <= 0` no longer deadlocks the message phase.**
  `migrator/messages.py` built `asyncio.Semaphore(config.max_concurrent_channels)` unguarded —
  `Semaphore(0)` admits no worker and hangs forever. Now clamped `max(x, 1)`, matching the
  existing defensive clamps for `checkpoint_interval` and `max_concurrent_requests`. Locked by
  a `wait_for`-bounded regression test.

### Documentation

- CLI reference: the "Internal defaults (not currently configurable)" section is gone — the
  seven settings are documented as real `migrate` flags. `gui-walkthrough` documents the
  16-control Advanced Options panel. `large-servers`, `pre-flight-checklist`, and
  `self-hosted-tips` regain the concurrency/reaction/checkpoint/thread-filter tuning advice
  that PR #98 had to remove, now phrased against real flags.
- **User-facing docs caught up with the code (v2.2.3 → v2.6.17)** (shipped unversioned as
  PR #98, folded into this release). The README, CLI reference,
  and guides had drifted ~14 releases behind. Highlights: the `ferry probe` command and
  post-migration invite generation are now documented; the "what gets migrated" tables include
  the native-fidelity work (role hoist/icons/live discovery, server description & NSFW,
  category ordering, slowmode, voice user limits); "role icons not migrated" and "slowmode
  not supported" removed from known limitations (both migrated since v2.5.0); all references
  to GUI Advanced Options controls that do not exist (reaction mode, min thread messages,
  checkpoint interval, concurrency, skip avatars, validate after) replaced with an honest
  "internal defaults, not currently configurable" note; wrong report filename `report.json`
  corrected to `migration_report.json`; `export-blueprint` options corrected (`--output` is
  required; `--name` exists); README gained badges and a section on the non-migration
  commands.

### Internal

- **Removed dead `_patch_checksums` test helper** (`tests/test_exporter_manager.py`). It was defined but never called (every `TestVerifyDceChecksum` test inlines its own `patch("importlib.resources.files")`), and was broken anyway — it built a `_Ctx` context manager then ignored it and returned a fresh, unconfigured patcher, papered over with `# type: ignore[return]`. No behavior change; suite stays at 926 passed.

## [2.6.17] - 2026-06-28

### Security

Cleared 3 high-severity Dependabot alerts — denial-of-service vulnerabilities in transitive
dependencies pulled by `nicegui[asyncio-client]` (the GUI shell). `uv.lock`-only bump; no
`pyproject.toml`, source, or API change.

- **`python-socketio` 5.16.1 → 5.16.3** — binary-attachment accumulation DoS
  (GHSA-5w7q-77mv-v69f, patched 5.16.2).
- **`python-engineio` 4.13.1 → 4.13.3** — unbound thread allocation DoS
  (GHSA-cgwc-pv48-fhj5, patched 4.13.2) + maximum payload size sometimes not enforced DoS
  (GHSA-m9gh-vj53-gvh9, patched 4.13.2).

## [2.6.16] - 2026-06-28

### Fixed

Defense-in-depth & misc lows — the tenth and FINAL batch from the 2026-06-25 v2.6.6
whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 10), closing the
hunt. Four latent-gap fixes; no GUI/CLI/state-schema/API change.

- **Engine exception strings are token-redacted everywhere they are emitted or persisted**
  (`core/engine.py`). A new `_safe(config, text)` helper wraps every raw-exception interpolation in
  the engine event stream and in persisted `state.warnings` / `RollbackFailure.error` — honoring
  `safe_sanitize`'s persist-or-emit contract. Previously only two sites were sanitized, leaving a
  latent path for a future token-in-URL or echoed-4xx-body to leak a credential to the GUI/CLI log
  or `state.json`. A new `_ensure_token_store` helper wires the token store in **both**
  `run_migration` and `run_rollback` (the shells call rollback directly and never set it), so the
  rollback-path redaction is actually effective. No behaviour change when no token store is set.
- **Concurrent uploads of the same file no longer duplicate** (`uploader/autumn.py`).
  `upload_with_cache` had a check-then-act race: two parallel channel workers requesting the same
  physical file (an author's avatar / identical attachment) both missed the cache and both uploaded,
  wasting the per-tag Autumn budget and orphaning a file. A self-cleaning in-flight `asyncio.Future`
  registry now coalesces concurrent same-key callers onto a single upload. A failed upload is not
  cached and does not poison the key (a later run retries); concurrent same-key callers share the
  originator's outcome.
- **Avatar checkpoint cadence is now time-based** (`migrator/avatars.py`). The phase saved state only
  every 10th upload, so a sub-10 tail (and a hard-kill between two saves) lost up to 9 `avatar_cache`
  entries, re-uploading those avatars as untracked orphans on resume. It now saves on a 5s wall-clock
  throttle (mirroring `messages.py`), bounding crash loss by time.
- **Repaired a dead test assertion** (`tests/test_thread_strategy.py`). `test_merge_mode_separator_sent`
  defined a capture callback that aioresponses never invoked, so the thread separator payload was
  never actually asserted. Converted to the `_capture_keys` idiom asserting the separator's
  idempotency key — a broken separator send now fails the test.

## [2.6.15] - 2026-06-28

### Fixed

Parser / exporter / sanitize correctness sweep — the ninth batch from the 2026-06-25 v2.6.6
whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 9). Ten confirmed
silent-correctness defects, grouped into five engine-side fixes; no GUI/CLI/state-schema/API change.

- **Bold runs are no longer corrupted by underline stripping** (`parser/transforms.py`). `strip_underline`
  collapsed every `****` in a message — so two adjacent bold spans (`**a****b**`) lost a delimiter
  (`**a**b**`), and `****` inside code spans was mangled. It now converts the two nested bold+underline
  forms explicitly and drops the blind global collapse, leaving pre-existing bold and code spans intact.
- **Timezone-naive timestamps are read as UTC** (`parser/transforms.py`). `format_original_timestamp`
  treated an offset-less DCE timestamp as host-local time (shifting the hour) and crashed on a
  malformed string; it now assumes UTC for naive inputs and falls back to the raw string on a parse
  error.
- **Channels are no longer misclassified as threads** (`parser/dce_parser.py`). `_infer_thread_info`
  treated any `Guild - Channel - Thread`-shaped filename as a thread, so a guild or channel name
  containing ` - ` mis-tagged a normal channel as a thread (corrupting category placement, merge/
  skip-threads handling, and forum grouping — and potentially dropping messages). It is now
  authoritative on the parsed Discord channel type (10/11/12 = thread). The dead `_TWO_SEGMENT_RE`
  regex was removed.
- **A huge DCE stderr line no longer crashes the exporter** (`exporter/runner.py`). `_read_stderr`
  used `async for`, which raised an uncaught `ValueError` on a stderr line over 64 KiB — stopping
  stderr capture, discarding the fatal line (reported as "Unknown error"), and leaking a
  "Task exception never retrieved". It now mirrors the stdout drain loop (truncating the over-long
  line). Cancellation is also checked during a long drain, and over-long lines now count as activity
  (no false "no new output" heartbeats).
- **Emoji name de-duplication no longer collides** (`migrator/sanitize.py`). `sanitize_emoji_name`
  truncated the disambiguating suffix off at the 32-char limit (returning a duplicate) and never
  registered the generated name (so a later identical name re-collided). It now truncates the base to
  make room for the suffix, registers the emitted name, and bumps until unique.
- **Six dropped role permissions are now migrated** (`discord/permissions.py`). `DISCORD_TO_STOAT`
  omitted Discord permissions that have Stoat equivalents, so migrated roles silently lost
  invite / kick / ban / manage-webhooks / change-nickname / manage-nicknames authority. Those six are
  now mapped (and added to the admin-grants-all set). (Discord's `MENTION_EVERYONE` has no Stoat
  `Permission` equivalent and remains intentionally dropped.)

## [2.6.14] - 2026-06-28

### Fixed

GUI lifecycle — the eighth batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt
(`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 8). Two GUI-shell-only fixes in
`gui.py`; no engine/parser/CLI/state-schema changes. Both close documented residuals of the
v2.6.1 token-lifecycle rework.

- **The cached-export choice is now honoured** (`gui.py`, S1/F8a). On `/export`, the
  "Use Cached"/"Re-export" choice card was inert: `background_tasks.create(_run_export())` ran
  unconditionally on page load, racing the click — re-validating the token, re-downloading DCE,
  re-running the export and overwriting the cached JSON, then navigating away. The auto-launch is now
  gated behind `_should_auto_export(cached)` (fires only when no cache exists); "Use Cached" goes
  straight to `/validate` (and clears the now-unneeded Discord token — no skip-path leak), while
  "Re-export" launches the export explicitly. The cached fast-path works and the cached JSON is no
  longer clobbered.

- **Session tokens are never persisted to disk** (`gui.py`, S2/F8b, security). The Stoat and Discord
  tokens were written to `app.storage.user`, which NiceGUI persists to `.nicegui/storage-user.json`
  on disk; the only clear ran at the terminal migration screen, so abandoning the flow (or a crash)
  before migration started stranded a plaintext token on disk — and the setup form even pre-filled
  the token fields from that on-disk copy. Both token values now live only in NiceGUI's memory-only
  per-tab store (`app.storage.tab`): never written to disk, dropped when the tab closes, and
  surviving in-tab navigation so the setup→export→validate→migrate handoff is preserved. This closes
  the abandonment window in every mode, including a hard crash. The token input fields no longer
  pre-fill (re-enter each launch — the `security.md`-aligned norm), a setup-load scrub removes any
  token left on disk by a pre-fix version, and missing-token restart edges bounce cleanly to setup
  with a "session expired" notice. Engine/shell separation is unchanged (the engine never reads
  `app.storage`).

## [2.6.13] - 2026-06-27

### Fixed

Incremental edge cases — the seventh batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt
(`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 7). Three engine-side fixes
(`migrator/messages.py`, `migrator/structure.py`, `reporter.py`); no GUI/CLI/state-schema changes.
All three are follow-up gaps in/around the v2.6.x incremental machinery (durable `channel_high_water`
v2.6.3, the #76 failed-message self-heal v2.6.4, reporting-integrity v2.6.2).

- **Merge-strategy thread failures are no longer permanently lost** (`migrator/messages.py`, S1/F7a).
  In `thread_strategy="merge"`, a message whose POST failed was recorded only as a warning, never a
  `FailedMessage`, while the high-water marker was written regardless — so on a later `--incremental`
  run the failed id sat below the marker and was skipped forever, unrecoverable by `ferry retry`. The
  shipped #76 self-heal is now ported into the merge loop: a POST failure is recorded as a
  `FailedMessage` (in all modes); on `--incremental` a prior-failed id is excluded from the skip gate
  and re-attempted; a completion reconciliation drops ids that succeeded this run and collapses a
  carried + re-fail duplicate to one entry. The high-water marker write is unchanged
  (plain/resume byte-identical). Both merge warnings (separator + message) are now sanitized via
  `safe_sanitize` so a token-bearing exception is never persisted to `state.json`. **Known
  limitation:** the `--incremental` re-attempt is the idempotency-safe primary recovery; recovering a
  *partial-success multi-part* merge message via `ferry retry` may re-send already-delivered parts
  (the `ferry-merge-` vs `ferry-` idempotency-namespace difference) — a proper fix needs the merge
  context threaded into the retry engine and is out of scope for this batch.
- **Incremental category sync no longer transiently deletes carried categories** (`migrator/structure.py`,
  S2/F7b). `run_categories` built its early full-replace category PATCH from only this run's (partial)
  export, so in incremental mode introducing a new category deleted carried-only categories until the
  CHANNELS phase re-PATCHed — a window where a crash/cancel left the live server wrong. The early
  upsert is now skipped in incremental mode; `run_channels`' authoritative upsert (which enumerates the
  full `category_map`) is the single source of truth. Fresh (non-incremental) runs are unchanged.
- **Fidelity scores can no longer render negative** (`reporter.py`, S3/F7c). In incremental mode the
  messages denominator is this run's partial `source_messages_total` while `failed_count` is carried
  cumulatively, so `compute_fidelity_score` could emit a negative Messages percentage (e.g. -60%) and
  depress the overall score. All five ratios are now clamped to `[0, 1]` via a `_clamp01` helper — a
  no-op for in-range inputs. The single helper change also fixes `ferry stats` (it delegates to the
  same function).

## [2.6.12] - 2026-06-27

### Fixed

Rate-limit hardening — the sixth batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt
(`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 6). Two engine-side HTTP clients
(`migrator/api.py`, `discord/client.py`); no GUI/CLI/state changes. Root cause: `await resp.json()`
was called unguarded inside the 429 (and Stoat's 2xx) branch, so a non-JSON body from a proxy/CDN
(Cloudflare HTML) raised `aiohttp.ContentTypeError` — a `ClientError` subclass — that escaped into
the generic network-error path.

- **A rate-limited Stoat request no longer trips the circuit breaker** (`migrator/api.py`, S1/F6a).
  A 429 with a non-JSON body fell through to the network-error handler, incremented
  `consecutive_failures`, and ignored the server's `Retry-After` — violating the code's own
  "429 is not a circuit-breaker failure" invariant. The 429 delay is now resolved by a
  content-type-guarded helper that honours the `Retry-After` / `X-RateLimit-Reset-After` headers
  (seconds) first, then the JSON body `retry_after` (Stoat milliseconds), then a 1s fallback — all
  capped at 60s. A 429 now never primes the breaker, **even when it exhausts the retry budget**
  (the final-attempt failure bump is skipped for 429); a 5xx / genuine network error still primes it.
- **A non-JSON Stoat 2xx now fails with a clear, correctly-classified error** (`migrator/api.py`,
  S2/F6b). A 200/201 with an HTML body was retried and re-raised as "Network error after 3 retries",
  hiding the real cause and mis-routing rollback classification. It now raises a distinct
  `MigrationError` naming the content-type; the "Network error after 3 retries" message is preserved
  for genuine connection errors.
- **Discord rate limits are honoured with a separate, bounded budget** (`discord/client.py`, S3/F6c).
  Both metadata getters read the 429 delay from the JSON body and shared the 3-attempt network-retry
  budget, so a Cloudflare HTML 429 was retried with a fixed 1s (ignoring a 60s+ `Retry-After`) and
  three such retries aborted the metadata-fetch phase. The two byte-identical getters are unified
  into one `_discord_request` that honours the `Retry-After` header, retries 429s on a separate
  bounded budget (`_MAX_429_RETRIES`) that no longer consumes the network-error budget, and
  content-type-guards both the 429 and the 200 body parse.

**Behaviour note:** behaviour-preserving for well-formed JSON responses and genuine network errors
(the "Network error after 3 retries" message is unchanged for real `ClientError`s). The change makes
the clients robust to non-JSON 429/2xx bodies from reverse proxies/CDNs and ensures the server's
`Retry-After` is respected. The circuit-breaker thresholds and the adaptive rate-multiplier are
unchanged. As a side benefit, an exhausted HTML-429 that was previously misclassified as a
network error (no HTTP status) is now correctly classified as a 429.

## [2.6.11] - 2026-06-27

### Fixed

CLI display & blueprint round-trip robustness — the fifth batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 5). All in the CLI shell (`cli.py`); the engine, migrators, GUI shell, and blueprint module are unchanged (the GUI shell was audited and uses no Rich markup and has no blueprint-build path, so none of these findings apply to it).

- **A markup-hostile channel/server/guild name can no longer abort a migration** (`cli.py`, S1/F5a). `Console` renders with markup enabled, so a Discord name containing Rich metacharacters (`spam[/]`, `[bold]news`, an unbalanced `]`) interpolated into a progress line raised `rich.errors.MarkupError`. Because `_ProgressTracker.on_event` runs synchronously inside the engine's `emit`, that exception unwound into the engine and was re-raised as a `MigrationError`, aborting an otherwise-healthy migration over a cosmetic display string. Every user-controlled value (event message, channel name, server/guild name, warnings, exception text, rollback suspect names, failure errors, the `ferry stats` error/warning previews) is now escaped via a new `_safe()` helper before markup interpolation; static format tags and integer/ID values are left live. A narrow `except MarkupError` guard around each tracker's render body is a defense-in-depth net (it falls back to an unstyled print and never swallows control flow — the rollback confirm/pause path is deliberately kept outside it).
- **`build` no longer aborts mid-build and orphans a server on a voice-channel failure** (`cli.py`, S2/F5b). The blueprint `build` command POSTed `channel_type="Voice"` with no error handling; a voice-create failure (Stoat "Bug #194") propagated and `sys.exit`ed after the server, roles, and earlier channels were already created — leaving an orphan server with no rollback. A new `_build_blueprint_channel()` helper mirrors the main migration path's voice→Text fallback (retry the channel as Text with a warning; non-voice errors still propagate); both build loops use it, and the categorized loop preserves the recovered channel's id into its category. This also un-breaks `ferry build --template gaming|community|education`, whose shipped templates contain Voice channels.
- **`build` now replays a blueprint's role hierarchy** (`cli.py`, S4/F5d). `BlueprintRole.rank` survived the export→import JSON round-trip but the build role loop applied only colour and permissions, so every built role got Stoat's default rank and the hierarchy was silently lost. The role loop now folds colour + rank into a single `api_edit_role` PATCH. This also restores the shipped templates' role ranks (Admin/Moderator/Member ordering).
- **`export-blueprint` is unchanged** (S3/F5c, decision A1). A Discord voice channel is still exported as `type="Voice"`, consistent with the migration path and the shipped templates; S2's new `build` fallback makes that value build-safe.

Internal: the build command's six Stoat API helpers were hoisted from a function-local import to module level (no import-cost change — the module already loads them via `run_migration`), giving the new module-level channel helper access to them and a single uniform mock-patch target for tests.

**Behaviour note:** S1 is a robustness fix (a hostile name no longer crashes the run) and is otherwise display-preserving; S2 narrows the abort surface (voice failures now recover; all other errors behave as before). **Known limitations:** (1) a hand-authored blueprint that sets an *explicit* `rank: 0` will not have that rank replayed — rank 0 is treated as "unranked" and left at Stoat's default (the shipped templates all use rank ≥ 1, and `export-blueprint` never emits roles, so neither is affected); (2) a *non-voice* channel-create failure during `build` still aborts mid-build and leaves a partial server (pre-existing behaviour, unchanged — S2 only recovers the voice-create case; orphan-server teardown is out of scope).

## [2.6.10] - 2026-06-27

### Fixed

Reaction/emoji fidelity-visibility cluster — the fourth batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 4). All in `migrator/emoji.py`, `migrator/reactions.py`, `migrator/messages.py`, `reporter.py`, `stats.py`, and `state.py`. Theme: reactions/emoji were dropped correctly (per Stoat's caps / missing assets) but with no durable signal, so the reaction fidelity score reported 100% while reactions were silently lost.

- **Unmapped-emoji reactions are no longer silently dropped** (`migrator/messages.py`). A native-mode custom reaction whose emoji never entered `emoji_map` (no uploadable asset, or beyond the emoji cap) hit a branch with no `else` — the reaction vanished with no counter or warning. It now increments a durable `reactions_dropped` counter (on the per-channel `ChannelResult`, folded into state; the retry/direct-state path writes state directly) and records a token-safe `unmapped_emoji_reaction` warning.
- **Emoji discovery upgrades a stranded record on a better-asset re-encounter** (`migrator/emoji.py`). First-seen-wins discovery stored `image_url=''` for an emoji first seen in message content/embeds; a later reaction carrying the real downloaded asset was discarded (the emoji was then skipped at upload and never entered `emoji_map`, compounding the dropped-reaction loss). A new `_record_emoji` helper upgrades the stored record in-place when the stored `image_url` is empty and the new source has a usable path (never downgrades), across all three discovery sources.
- **Emoji-cap truncation ranks by uploadability then usage, not a lexicographic accident** (`migrator/emoji.py`). When more than `max_emoji` unique emoji are found, the kept subset was the first N by `str(id)` — an arbitrary lexicographic slice that could drop high-traffic emoji while keeping rare ones. The kept set is now ranked uploadable-first (an asset-less emoji can never occupy a slot ahead of a creatable one), then by occurrence frequency, with a non-numeric-id-safe tie-break; the truncation warning names the dropped emoji. A `_EmojiRecord` `TypedDict` keeps the occurrence tally `mypy --strict`-clean.
- **Cap-skipped reactions are counted in the fidelity denominator** (`migrator/reactions.py`). At Stoat's 20-reactions-per-message hard cap the loop emitted an ephemeral warning then `continue`d without counting, so cap-skipped reactions were in neither `reactions_applied` nor `pending_reactions` — invisible to the score. A durable `reactions_capped` counter now records them.
- **Reaction fidelity now reflects capped + dropped reactions** (`reporter.py`, `stats.py`). The reaction denominator (`reactions_total`) is assembled at the report call sites as `reactions_applied + reactions_capped + reactions_dropped + len(pending_reactions)` (no `compute_fidelity_score` signature change). This also fixes the `stats.py` None-gate: a run whose only reactions were dropped (applied 0, pending empty, dropped > 0) now reports **0%** reaction fidelity instead of "N/A". Two new persisted `MigrationState` counters (`reactions_capped`, `reactions_dropped`) deserialise from old `state.json` as 0.

**Behaviour note:** this is a **reporting** change, not a migration-behaviour change — the same reactions/emoji are migrated/dropped as before (Stoat's 20-reaction and 100-emoji caps are unchanged); the reaction fidelity percentage now reflects the loss instead of hiding it, and the emoji kept under the cap are chosen by uploadability + usage rather than id order.

## [2.6.9] - 2026-06-26

### Fixed

Message-phase control-flow cluster — the third batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 3). All in `migrator/messages.py` and `core/engine.py`. Common root: `asyncio.gather(*tasks, return_exceptions=True)` collapsed every `BaseException` (including `asyncio.CancelledError`) into a swallowed warning, and the `_process_message` retry path returned instead of re-raising.

- **`ferry retry` no longer hangs or silently drops a still-failing message** (`migrator/messages.py`, `core/engine.py`). On the retry path (`_process_message` called with `channel_result=None`), a send failure was caught, appended to `state.failed_messages`, and the function **returned without re-raising**. The retry loop `for fm in state.failed_messages` then (a) appended to the very list it was iterating → mutate-during-iteration → an infinite loop on a deterministically-failing message, and (b) counted the re-failure as `retried` (reported success) and dropped it. `_process_message` now re-raises on failure **only when `channel_result is None`** (the parallel per-channel path keeps degrade-in-loop), and `run_retry_failed` iterates a `list(...)` snapshot — so the retry terminates, keeps the message failed with `retry_count` incremented, and accounts it correctly.
- **User cancellation during the parallel message phase is now handled cleanly** (`migrator/messages.py`). `gather(..., return_exceptions=True)` returned a worker's `asyncio.CancelledError` as a result item, which the result loop logged as a `channel_worker_failed` warning before emitting the phase `completed` event — so the engine's clean-cancel handler never ran. The result loop now detects `CancelledError` first (it is a `BaseException`, not an `Exception`) and re-raises `asyncio.CancelledError` after checkpointing the channels that finished before the cancel, so the engine saves state and reports "Cancelled during messages". The in-loop cancel check also raises instead of `break`-ing, so a cancelled-mid-channel worker no longer falls through to self-marking the channel complete (which would lose its un-sent tail on `--resume`).
- **A channel worker that crashes before its per-message loop is no longer silently lost** (`migrator/messages.py`, `core/engine.py`). Such a crash was recorded as a warning while the phase reported `completed` and `current_phase` advanced past `messages`, so on `--resume` the channel was skipped — never migrated, never retried, no DLQ entry. The result loop now collects the first non-cancel exception and re-raises it after checkpointing the successful channels, so the phase fails with `current_phase` still `"messages"`; `--resume` then re-runs the crashed channel while already-completed channels skip via `completed_channel_ids`. All surfaced failure strings are sanitised once via `safe_sanitize` and reused for both the persisted error and the emitted event (no raw exception/token in the event).

**Behaviour change:** a channel worker raising an unexpected exception **before** sending any message now **aborts the run** (resumable via `--resume`) instead of degrading to a warning and reporting the phase `completed`. This trades a silent, unrecoverable per-channel data loss for a surfaced, resumable failure. A single message failing *inside* a channel's send loop still degrades-in-loop (recorded as a `FailedMessage`, channel continues) exactly as before.

## [2.6.8] - 2026-06-26

### Fixed

Resume & migration-lock integrity cluster — the second batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 2). All in `core/engine.py`, `migrator/structure.py`, and `state.py`.

- **The advisory migration lock now survives the SERVER phase** (`core/engine.py`, `migrator/structure.py`). The S17 lock is a `[FERRY_LOCK:{ts}:{host}]` marker appended to the live server description by `_acquire_migration_lock`; the SERVER phase then PATCHed the description with the Discord guild description — a full-field replacement that **wiped the marker**, so the lock protected only the connect+server phases and a second concurrent existing-server migration saw no lock during the hours-long message/reaction/pin tail. The engine now stashes the marker it wrote into a transient `state.migration_lock_marker` (never persisted, re-acquired each run), and `run_server` folds it into the description it already PATCHes — no extra API call. `_release_migration_lock` clears the field.
- **A `--resume` after a kill mid-SERVER no longer creates a duplicate server** (`migrator/structure.py`). `run_server` set `state.stoat_server_id` from `api_create_server` but only the engine's post-phase save persisted it; a kill in that window left an on-disk state with no server id, so `--resume` re-entered the create branch and made a second server (orphaning the first with all its channels/roles). The id is now persisted with `save_state` immediately after creation, so `--resume` takes the reuse branch.
- **`run_roles` resume now finalizes attributes and permissions for roles created before a crash** (`migrator/structure.py`, `state.py`, `core/engine.py`). `pre_existing_role_ids = set(role_map)` (captured at phase start) gated all three role passes, so on resume the roles created in the prior run were treated as pre-existing and their rank/hoist/icon/permissions were **never applied**. A new persisted `state.roles_finalized` set now gates the attributes + permissions passes (the create pass still gates on `pre_existing_role_ids` to avoid duplicate creation); roles are marked finalized at the **end** of a completed `run_roles` (regardless of the metadata-gated permissions pass), so a crash before the end re-runs both passes idempotently on resume. `roles_finalized` is carried into `--incremental` state and back-compat-seeded from `role_map` for migrations completed by an older version, so incremental runs still skip re-editing. A periodic intra-phase save in the create loop gives hard-kill durability.

## [2.6.7] - 2026-06-25

### Fixed

Autumn upload robustness cluster — the first batch from the 2026-06-25 v2.6.6 whole-codebase bug-hunt (`docs/plans/audits/2026-06-25-bug-hunt.md`, §4 Batch 1). All in `uploader/autumn.py` and its callers.

- **A malformed Autumn `200` no longer aborts the migration with a raw exception** (`uploader/autumn.py`). The success path did `await response.json()` + `result["id"]`, so a non-JSON `200` (reverse-proxy HTML, empty body) raised a raw `aiohttp.ContentTypeError` / `KeyError`, and an empty body would raise `TypeError`. Because `_resolve_role_icon` (`migrator/structure.py`) caught only `AutumnUploadError`, such a raw error escaped its guard and **aborted the entire migration** for any server with custom role icons. The body is now parsed with `json(content_type=None)` inside `try/except (aiohttp.ClientError, ValueError)` and an explicit `isinstance(result, dict) and "id" in result` check; every malformed `200` raises `AutumnUploadError` (carrying only the tag — never the response body or token).
- **`--verify-uploads` now actually detects a corrupt upload** (`uploader/autumn.py`). On a present-and-mismatched server `size`, `upload_to_autumn` previously popped the cache entry but still **returned** the bad id, which `upload_with_cache` immediately re-inserted — the whole feature was a no-op that propagated the corrupt id forever. A size mismatch is now treated as a failed upload (raises `AutumnUploadError`; never returned, never cached). The now-dead `cache` parameter was removed from `upload_to_autumn`.
- **A non-JSON `429` no longer crashes the retry path, and `Retry-After` is honoured** (`uploader/autumn.py`). The 429 branch did `await response.json()` assuming a JSON body; an HTML `429` from a reverse proxy raised `ContentTypeError` that bypassed backoff and (in the sticker/attachment loop) dropped the media. A new module-private `_retry_after_ms` computes the backoff defensively: body `retry_after` (ms) → `Retry-After` header (integer seconds) → 1000 ms default, tolerant of any non-JSON body.
- **Role-icon upload failures degrade instead of aborting** (`migrator/structure.py`). `_resolve_role_icon`'s guard is broadened to `except (AutumnUploadError, OSError)` (also covering the temp-file write), so an icon that can't be uploaded is skipped with a token-safe warning and the roles phase continues — rank/hoist for that role still apply.
- **Sticker and embed-media uploads reach attachment parity** (`migrator/messages.py`). Both now pass `verify_size=config.verify_uploads` and register in `state.autumn_uploads` like regular attachments. The embed `media_id` is additionally credited to `referenced_autumn_ids` on a successful send (but **not** added to `autumn_ids`, so it does not consume the 5-attachment cap) — without this it would have been reported as a false orphan on every migrated embed image.

## [2.6.6] - 2026-06-25

### Fixed

- **Empty-message fallback no longer silently clobbers poll/sticker/placeholder/embed-note bodies** (`migrator/messages.py`). From the 2026-06-24 v2.5.1 bug-hunt refresh (HIGH-severity silent content data loss). The Step 6 empty-message guard tested the **raw** `msg.content == ""`, but `_build_content` and the caller append several optional bodies into the built `content` *before* that guard — poll text (`flatten_poll`), sticker text (`handle_stickers`), oversized/expired-attachment placeholders, and the "[N embed(s) could not be migrated]" note. A message whose entire body came from one of those paths (e.g. a poll-only or sticker-only message, which has empty raw content) was therefore overwritten with `[empty message]` — silent loss the user never saw. The guard now tests the **built** `content`: it compares `content.strip()` against a reconstructed empty baseline (the timestamp prefix plus, for edited messages, the `*(edited)*` marker), so any pre-guard append path is preserved. The `not autumn_ids and not stoat_embeds` conditions are retained, so attachment-only and embed-only messages are still **not** labeled empty, and a whitespace-only message is now (correctly) treated as empty. Empty **edited** messages are relabeled `[empty message] *(edited)*` instead of dropping the edit marker. The `" *(edited)*"` literal was extracted into a shared `_EDITED_MARKER` constant so `_build_content` and the guard stay byte-identical.

## [2.6.5] - 2026-06-25

### Fixed

- **Thread `merge` strategy now honours the durable high-water marker** (`migrator/messages.py`). Closes GitHub #78 (finding M2). The v2.6.3 incremental gate covered the default `flatten` strategy (via `_process_single_channel`), but `thread_strategy="merge"` runs a separate function (`_merge_threads`) that re-POSTed the separator + **every** thread message into the parent channel on **every** `--incremental` run — idempotency-keyed so it never duplicated content, but wasted O(all-thread-messages) HTTP work each run. `_merge_threads` now: skips the separator and already-copied messages on `--incremental` (gated on `config.incremental`), tracks the thread's max message id (over all messages incl. system/skip-type ones, `isdigit()`-guarded), and writes a per-thread `channel_high_water` marker at completion (every run, so a first full run sets it up). An unchanged merged thread therefore makes **zero** POSTs on an incremental run, matching `flatten`; a thread with K new messages re-POSTs only those K (no re-sent separator). `flatten`, `archive`, and `--resume` behaviour are unchanged. The per-channel completion event now reports the count actually posted this run.

## [2.6.4] - 2026-06-25

### Fixed

- **Incremental mode now self-heals previously-failed messages, and a non-numeric carried offset no longer crashes the skip gate** (`migrator/messages.py`, `core/engine.py`). Closes the v2.6.3 **known limitation** (GitHub #76) and a folded-in pre-existing crash (GitHub #77), both surfaced by the whole-branch review of the v2.6.3 work.
  - **A message that failed to POST on a prior run is now re-attempted automatically on the next `--incremental` run** (#76). The durable `channel_high_water` mark tracks the highest message id *seen*, not *successfully copied*, so a failed id sat below the mark and was skipped forever — and the engine carry-over reset `failed_messages`, wiping its retry record. Now the incremental carry-over **carries `failed_messages` forward** (as independent copies), the within-channel skip gate **excludes previously-failed ids for that channel** so the messages phase re-POSTs them, and a completion-time **reconciliation** drops ids that succeeded this run and collapses the carried + fresh-re-fail duplicate to a single entry (so the failure count stays stable across runs). Running `ferry retry` before `--incremental` is **no longer required** — the v2.6.3 known-limitation note is resolved. `--resume` is unaffected (it does not consult `failed_messages`); an unchanged channel with no prior failures still makes zero POSTs.
  - **A non-numeric carried offset no longer fails the channel worker** (#77). The skip threshold used `max(…, key=int)` / `int(_skip_below)`, but the periodic checkpoint persisted the raw `msg.id`, which can be non-numeric for some system messages; a prior run that crashed right after such a checkpoint then raised `ValueError` (swallowed by `asyncio.gather` → the channel was silently skipped). The threshold now filters/normalizes non-numeric values to "no threshold" (copy, never crash) for both `--incremental` and `--resume`, and the checkpoint write is guarded with `isdigit()`.
  - The incremental per-channel completion event now distinguishes re-attempted failures from new messages (`"{n} new, {m} already present, {k} retried"`; the `retried` clause is omitted when zero, byte-identical to the prior message).

### Notes

- A permanently-unsendable message is re-attempted (one idempotent POST) on each incremental run, matching the existing `ferry retry` semantics (no retry cap); `retry_count` is preserved if a cap is ever wanted. Edited/deleted-message sync remains out of scope.

## [2.6.3] - 2026-06-25

### Fixed

- **Incremental mode now copies only NEW messages** (`migrator/messages.py`, `core/engine.py`, `state.py`). From the 2026-06-24 v2.5.1 bug-hunt refresh — this closes the two items the v2.6.2 entry flagged as "tracked separately" (the incremental message-offset re-stream and the forum-index count double-count).
  - **`--incremental` no longer re-streams and re-POSTs the entire history every run.** The within-channel skip (`messages.py`) was gated solely on `config.resume`, but `--incremental` and `--resume` are mutually exclusive, so the skip never fired for incremental runs — every run did O(all-messages) work. Server-side idempotency (`Idempotency-Key=ferry-{msg.id}`) hid the duplicate POSTs, so the symptom was invisible: no speedup, ever.
  - **Durable per-channel high-water mark** (`MigrationState.channel_high_water`, persisted): the existing `channel_message_offsets` is transient within-run resume state and is popped on channel completion, so after a clean run there was nothing to skip from. The new field records the highest copied message id per channel, is written at channel completion (never popped), is carried into incremental runs by the engine, and is consulted by a mode-aware skip gate — resume keeps using the transient offset (unchanged), incremental uses `max(high_water, carried_offset)` so a crashed prior run degrades gracefully. Back-compat: an old `state.json` lacking the field loads with `{}` and the run falls back to a (harmless, idempotent) full re-copy.
  - **The thread/forum header POST is now gated on the same marker**, so an unchanged thread/forum channel makes zero POSTs on an incremental run (previously it re-POSTed `[Thread migrated from #…]` every run, idempotency-keyed only).
  - **The forum-index message counter no longer inflates across incremental runs.** It double-counted because re-processed messages re-incremented `channel_message_counts`; once messages stop re-processing the count holds — fixed for free, locked in by test.
  - Replaced two false-green delta tests (`tests/test_delta_migration.py`): a `... or True` tautology and an assertion against the wrong (avatar) value. Added real-production-code guard tests (`tests/test_messages.py`) that drive the real messages phase against mocked Stoat endpoints and fail if the skip gate or the carry-over is removed.

### Notes

- Edited/deleted-message sync is explicitly out of scope — a high-water mark can only detect appended messages, not changes to already-copied ones.
- **Known limitation — run `ferry retry` before `--incremental`.** A message that failed to POST on a prior run (recorded in `failed_messages`) is below the channel's high-water mark, so a later `--incremental` skips it and does not auto-retry it; `failed_messages` is also reset per incremental run, so its retry record is lost. Retry such failures with `ferry retry` against the prior `state.json` *before* running an incremental update. (Previously, the broken incremental re-streamed everything and thus re-attempted failures as a side effect; that accidental self-heal is gone now that re-streaming is fixed.) A proper fix — preserving failed-message retry across incremental runs — is tracked as a follow-up.

## [2.6.2] - 2026-06-24

### Fixed

- **Reporting integrity: post-migration reports were wrong on every run** (reaction/pin counters + message-fidelity denominator). From the 2026-06-24 v2.5.1 bug-hunt refresh; affects all three reporting surfaces (`ferry stats`, `migration_report.json`, the markdown report).
  - **Reaction fidelity is no longer stuck at 50%** (`migrator/reactions.py`, `reporter.py`, `stats.py`): `state.pending_reactions` was never cleared, so every surface computed `reactions_total = reactions_applied + len(pending_reactions) = 2N` after a fully successful run → a flat 50% reaction sub-score. `run_reactions` now consumes `pending_reactions` as a work queue (applied entries removed, failed retained, cap-skipped removed), so the derived total self-corrects to N (100%).
  - **Resume no longer double-counts** (`migrator/reactions.py`, `migrator/pins.py`): the engine re-runs the in-progress phase on resume; with the pending list never consumed, `reactions_applied`/`pins_applied` re-incremented over the persisted value. The phases now checkpoint progress via `save_state` (periodic, mirroring the messages phase) so a resumed phase processes only the remainder. A new persisted `MigrationState.reaction_message_counts` keeps the 20-reactions-per-message cap exact across a resume.
  - **`ferry stats` no longer reports 0% Messages fidelity on a normal run** (`stats.py`, `core/engine.py`, `state.py`): `summarize_state` used the incremental-only `prior_messages_total` (default 0) as the message denominator. A new persisted `MigrationState.source_messages_total` (set by the engine from the post-filter export total, on every run incl. resume) is now the shared denominator for both `ferry stats` and the JSON/markdown report (legacy fallback to imported+failed / `sum(exports)`), so the two surfaces agree.
  - **Dry-run** now reports 100% for reactions/pins (clears the pending list) instead of 50%.

### Notes

- Server-side state was always correct — Stoat reaction/pin PUTs are idempotent; these were purely local counter/stats bugs. Out of scope (tracked separately): the incremental message-offset re-stream (`migrator/messages.py`) and the forum-index count double-count (`core/engine.py`).

## [2.6.1] - 2026-06-24

### Fixed

- **GUI token lifecycle — two HIGH security/state-lifecycle defects in the NiceGUI shell** (`gui.py`), from the 2026-06-24 codebase bug hunt. Token cleanup is now centralised through a single `_clear_tokens(storage, keys)` helper and a `_SESSION_TOKEN_KEYS = ("token", "discord_token")` constant (`stoat_url` intentionally excluded — it is an instance URL, not a credential).
  - **Orchestrated export no longer clears the still-needed Stoat token.** `_run_export`'s `finally` cleared both the Discord token and the Stoat `token`, but the orchestrated flow immediately redirects to `/validate`, whose guard (and `/migrate`'s) requires `storage["token"]`. The synchronous `finally` runs before the queued redirect loads, so the 1-Click flow bounced the user back to setup with an empty Stoat field. The export `finally` now clears only `discord_token`; the Stoat token is cleared at the terminal migration screen.
  - **Offline-mode migration now clears the Stoat token from on-disk storage.** The migration `_run` had no `finally`, and offline mode (setup → /validate → /migrate, skipping /export) never reached the export cleanup — so the plaintext Stoat token persisted in `.nicegui/storage-user.json` after completion or error (a `security.md` violation). The migration `_run` gained a terminal `finally` that clears both tokens on success AND error, for both offline and orchestrated modes. Safe for the in-flight migration: `FerryConfig` holds its own token copy (built before the task), the engine never reads `app.storage`, and the only in-`_run` storage read precedes `run_migration`.
  - Guarded by a constant-pin unit test (`tests/test_gui.py`) that fails CI if the security-critical key list ever silently shrinks, plus helper behaviour tests.

### Notes

- Two non-terminal abandonment cases remain out of scope (tokens linger if the user opens `/migrate` or lands on `/validate` but never proceeds — fixing them needs client-disconnect hooks). Bounded by the single-user, local-only (`127.0.0.1`), gitignored-storage nature of the tool.

## [2.6.0] - 2026-06-24

### Added

- **Live role discovery — migrate every server role, not just roles whose members posted** (`migrator/structure.py`, `discord/metadata.py`, `discord/__init__.py`, `migrator/api.py`). Previously `run_roles` derived its role set from `msg.author.roles`, so a role only migrated if someone wearing it posted in an exported channel — empty/structural/staff roles silently vanished. When a `discord_token` is supplied, role creation now sources from the **union** of export-derived roles and the full live Discord role list (already captured in `discord_metadata.role_metadata`, which enumerates every non-managed, non-@everyone role). For a role present in both, the **live** name/colour/position win over the (possibly stale) export snapshot. `RoleMeta` gained `name` and `color` (normalised to hex at capture); a `_role_from_metadata` adapter synthesises a `DCERole` for live-only roles so all three existing role passes (create / rank / permissions) cover them unchanged.
- **Graceful Stoat role-cap handling.** Stoat enforces a configurable `server_roles` limit (default 200) that the wider role set can now reach. The live limit is read best-effort from `GET /` (`features.limits.global.server_roles`, via a new `api_fetch_root` wrapper, 200 fallback); if the union exceeds it, the lowest-priority roles by `(position, id)` are dropped with a single `role_limit_exceeded` warning, and a `TooManyRoles` create-time backstop stops gracefully rather than crashing the phase.
- **Reporter credit** (`reporter.py`): the `## Native Fidelity` section now reports the count of recovered "structural" roles (live-only roles absent from the export), counted per-run.

### Changed

- Role rank ordering now sorts by `(position, id)` (string id) for a deterministic tie-break when multiple roles share a Discord position — relevant now that the full live role list (with denser positions) feeds the rank pass.

### Notes

- Without a `discord_token` (no live metadata), role sourcing falls back to the prior export-derived behaviour byte-for-byte — same role set, same create order. Managed/integration roles and `@everyone` remain excluded. This does not assign members to roles (that requires a Discord→Stoat identity map, which does not exist); it only ensures the roles themselves are created.

## [2.5.1] - 2026-06-24

### Fixed

- **Incremental (`--incremental`) migration now reuses the prior server and structure instead of duplicating everything.** Fixes the dominant cluster from the 2026-06-23 codebase bug hunt: the structure phases had no idempotency guard, so every delta re-run re-created all roles/categories/channels and `run_server` minted a brand-new server (stranding the carried ID maps).
  - **Server reuse** (`migrator/structure.py`): `run_server` reuses the carried `state.stoat_server_id` (not only `--server-id`); a deleted prior server raises a clear `MigrationError` instead of silently creating a new server.
  - **Structure-phase idempotency** (`run_roles`/`run_categories`/`run_channels`): a capture-before-mutate `pre_existing_*_ids` snapshot skips already-migrated roles (across the create, attributes, and permissions passes), reuses carried category/channel IDs, preserves category membership (no orphaning), reuses forum-index channels, and excludes carried channels from the `--max-channels` truncation budget.
  - **Exact category membership** (`state.py`, `core/engine.py`): a persisted `channel_categories` map reconstructs carried-only categories exactly on a partial re-export (no cross-contamination).
  - **Carry-over completeness** (`core/engine.py`): the incremental carry-over now also carries `forum_channel_members`, `forum_category_names`, `forum_index_message_ids`, `channel_message_counts`, `category_names`, `channel_categories`, and `native_fidelity_counts`, with a field-by-field audit comment. `_rebuild_forum_indexes` PATCHes the existing pinned index with cumulative counts instead of posting duplicates.
  - Locked by `tests/test_incremental_structure.py` (real-phase two-run tests). The prior `tests/test_delta_migration.py` mocked the structure phases, which is why this shipped undetected.

## [2.5.0] - 2026-06-24

### Added

- **Native-fidelity batch 2: per-channel slowmode, voice user-limit, and image role icons.** Successor to the v2.3.0 batch (#62). Three more native Discord properties now migrate when a `discord_token` is supplied, all source-verified against `stoatchat/stoatchat` @ a15a542:
  - **Per-channel slowmode** (`migrator/structure.py`, `migrator/api.py`). Discord `rate_limit_per_user` is captured into `ChannelMeta.slowmode` and applied via a new `api_edit_channel` PATCH wrapper (routes through the existing rate-limited `_api_request`). Applied only when `> 0`, clamped to Stoat's `0..=21600` range with a `slowmode_clamped` warning. Slowmode is a flat top-level PATCH field.
  - **Voice channel user-limit** (`migrator/structure.py`). Discord voice `user_limit` is captured into `ChannelMeta.user_limit` and applied as `voice={"max_users": N}` (nested). Discord's `0` (unlimited) maps to an omitted `max_users` (Stoat rejects `0`). The voice PATCH is gated by a `created_as_voice` flag so it never fires on a channel that fell back to Text via the Bug #194 retry; slowmode still applies to such fallbacks.
  - **Image role icons** (`migrator/structure.py`, `discord/client.py`). A role's image icon is downloaded from the Discord CDN (`download_role_icon`, capped at the Autumn 2.5 MB icons limit), uploaded to Autumn under the `icons` tag, and folded into the existing role attributes pass as `DataEditRole.icon`. Unicode-emoji-only role icons are not migratable and are skipped with a warning. The Autumn upload failure is caught as `AutumnUploadError` and reported with a fixed, role-name-only message — never the raw exception body, which can echo the session token (`security.md`).
  - **Reporter credit + shell surfacing** (`reporter.py`, `cli.py`, `gui.py`, `state.py`). Applied counts accumulate in `MigrationState.native_fidelity_counts` (serialized for resume) and surface as `report["native_fidelity"]`, a `## Native Fidelity` markdown section, a CLI summary line, and a GUI completion-card label (hidden when empty).
  - **Graceful degradation.** With no `discord_token`, all three fields skip and emit distinct one-time `slowmode_skipped` / `user_limit_skipped` / `role_icon_skipped` warnings; migration completes and v2.3.0 fields are unaffected.

## [2.4.2] - 2026-06-23

### Fixed

- **Resume no longer crashes after a validated migration** (`core/engine.py`). When `validate_after` is enabled, `run_migration` persists `current_phase="validate_migration"` — a terminal phase that is not in `PHASE_ORDER`. A subsequent `--resume` called `PHASE_ORDER.index(state.current_phase)` in `_run_phases` with no membership check, raising `ValueError: 'validate_migration' is not in list` before any phase ran, aborting the resume entirely. The resume guard now treats any `current_phase` outside `PHASE_ORDER` as "all runnable phases complete" (`current_idx = len(PHASE_ORDER)`), so resume correctly skips the finished pipeline instead of crashing. Locked by `test_run_migration_resume_after_validate_does_not_crash`.
- **GUI progress bar no longer crashes on the post-migration validation event** (`gui.py`). The migration progress handler called `PHASE_ORDER.index(event.phase)` on every non-`report` `completed` event, but the engine emits a `phase="validate_migration"` completed event when post-migration validation passes — a value not in `PHASE_ORDER`, raising `ValueError` inside the event handler. Extracted a `_phase_progress(phase)` helper that returns `None` for post-pipeline phases; the handler skips the progress-bar update for them. Locked by `test_phase_progress_pipeline_phases` and `test_phase_progress_post_pipeline_phase_is_none`.

### Security

- **`dce_checksums.json` is now bundled into the frozen binary** (`ferry.spec`). The PyInstaller spec bundled only `templates/*.json`, so the shipped binary lacked the pinned DCE hashes. `exporter/manager._verify_dce_checksum` reads them via `importlib.resources`, and its `except (FileNotFoundError, ModuleNotFoundError): return` clause then **silently skipped** checksum verification — defeating the supply-chain hard-fail gate from v2.2.12, but only in the packaged app (source-run tests always had the file, so the suite never caught it). Added `("src/discord_ferry/dce_checksums.json", "discord_ferry")` to `all_datas`. Locked by `test_ferry_spec_bundles_dce_checksums`.

## [2.4.1] - 2026-06-23

### Security

- **Cleared all 14 open Dependabot alerts via three lockfile bumps** (no application code affected; all alerts were in `uv.lock`):
  - **aiohttp 3.14.0 → 3.14.1** (8 alerts). Ferry uses aiohttp as an HTTP *client*, so the server-side advisories (HTTP-parser `max_line_size` bypass, pipelined-request queue, websocket frame limits, `client_max_size`) do not apply; the client-side ones (DigestAuth cross-origin redirect credential leak GHSA-hpj7-wq8m-9hgp, cookie-jar domain promotion GHSA-2fqr-mr3j-6wp8, TLS hostname-override on connection reuse GHSA-4m7w-qmgq-4wj5) are patched. The `aioresponses` `stream_writer` shim in `conftest.py` is version-conditional and unaffected.
  - **starlette 1.2.1 → 1.3.1** (2 alerts, incl. HIGH GHSA-82w8-qh3p-5jfq — `request.form()` size limits silently ignored). Transitive via NiceGUI→FastAPI; powers the local-only GUI web server.
  - **python-multipart 0.0.28 → 0.0.32** (4 alerts, incl. HIGH GHSA-5rvq-cxj2-64vf — quadratic-time querystring parsing). Transitive via NiceGUI.
  Full suite green on the upgraded resolve.

## [2.4.0] - 2026-06-23

### Added

- **`probe` CLI subcommand — live-instance diagnostics**. `discord-ferry probe --test-server-id <id>` runs four read-mostly checks against a live Stoat instance and renders a Rich table (or `--json`): Autumn per-tag size limits vs. our assumptions, voice Bug #194 detection (this Revolt fork has no VoiceChannel variant — judged on the presence of a `voice` field, not the discriminator), webhook availability (judged on EXECUTE, which is mounted only when `features.webhooks_enabled` is true — default off), and rate-limit header capture. Every entity created under the throwaway test server is torn down in a `finally` block (capture-id-before-raise), and the probe never constructs or writes a `MigrationState`. New module `migrator/probe.py` (`run_probe`, `ProbeReport`).
- **Post-migration invite generation (S4)**. The migration now mints an invite to the new Stoat server during the REPORT phase and surfaces it in the CLI summary, `migration_report.json` (`invite` block), the markdown report (`## Invite`), and the post-migration checklist. Controlled by `--create-invite/--no-create-invite` (default on) and `--invite-channel-id`. `_select_invite_channel` picks a Text channel, excluding voice (Discord type 2), threads, and synthetic forum-index channels; forums (15/16 → Stoat Text) are eligible. The invite URL is built best-effort from the instance root's `app` field (bare code otherwise). Non-fatal on error (migration is already complete → warning, never raises), idempotent via an `invite_code` guard in both the engine caller and `_generate_invite`, and carried forward on incremental re-runs so resumes never re-mint. New `MigrationState.invite_code`/`invite_url` (serialised, forward-compatible). Failure warnings are sanitised through the token store before reaching `state.json`.

### Internal

- **Four probe-support API wrappers + `_headers(token: str | None)` widening** (`migrator/api.py`): `api_create_invite`, `api_create_webhook`, `api_fetch_channel`, `api_delete_webhook`, and an auth-leak-safe `api_execute_webhook` that passes `token=None` so the user's `x-session-token` is never sent to the URL-token-authenticated webhook-execute endpoint. Webhook wrappers are **probe-only** — webhook-based message posting is deliberately out of scope.

### Fixed

- **`icons` Autumn size limit corrected to 2_500_000** (`uploader/autumn.py`). Source check against stoatchat `Revolt.toml` (`features.limits.default.file_upload_size_limit`) shows a flat 2.5 MB, not the `2560 * 1024 = 2_621_440` we previously enforced — an icon between those sizes would pass our pre-check then be rejected by Autumn.

## [2.3.0] - 2026-06-23

### Added

- **Role hoisting is now migrated (S1)**. Discord's per-role `hoist` flag (which displays a role as a separate member-list group) was previously dropped — every migrated role collapsed into the default list. `fetch_and_translate_guild_metadata` (`discord/__init__.py`) now captures `hoist` (and `position`) into a new `RoleMeta` carrier on `DiscordMetadata`, and `run_roles` (`migrator/structure.py`) applies it. The former rank-only second pass became an **attributes pass** that folds `rank` and `hoist` into a single `api_edit_role` call per role, so hoist is applied even for roles with no colour and position 0 — and with **no extra API calls** versus before. `mentionable` is intentionally not migrated (no Stoat equivalent). Requires `discord_token`; without it, hoist is skipped with one warning (`hoist_skipped`). Locked by `test_run_roles_applies_hoist_when_metadata_present` and `test_run_roles_hoist_skipped_without_metadata`.
- **Server description and NSFW flag are now migrated (S2)**. Both are native Stoat fields that were never read from the source guild. `fetch_and_translate_guild_metadata` now captures `guild_description` and `guild_nsfw`, and `run_server` applies them via the existing `api_edit_server` PATCH (description sent only when non-empty). Skipped with one warning (`server_meta_skipped`) when Discord metadata is unavailable. Locked by `test_run_server_applies_description_and_nsfw`, `test_run_server_omits_empty_description`, and `test_run_server_meta_skipped_without_metadata`.
- **Categories are now ordered by their Discord position (S3)**. Categories previously rendered in DCE export-iteration order (effectively arbitrary). The authoritative second `api_upsert_categories` call in `run_channels` now sorts the categories array by each category's captured Discord `position` (new `DiscordMetadata.category_positions`, populated only from type-4 GUILD_CATEGORY channels). Forum-derived categories — keyed in `category_map` by a non-Discord forum key and thus without a position — sort to the end with a stable title tie-break. Falls back to the prior order when metadata is absent. Locked by `test_run_channels_orders_categories_by_discord_position` and `test_run_channels_category_without_position_sorts_last`.

### Changed

- **`DiscordChannel` now parses the channel `position` field** (`discord/models.py`, `discord/client.py`), previously discarded — needed for category ordering above.

### Internal

- **Extracted `_stoat_channel_type(int) -> str`** in `migrator/structure.py` as the single source of truth for the Discord-type → Stoat-type mapping (type 2 → Voice, else Text), replacing the inline match in `run_channels`.
- All three new metadata fields round-trip through `discord_metadata.json` with `.get()`-defaulted decodes, so pre-upgrade metadata files load forward-compatibly and trigger the graceful-skip path. Locked by a deep-equality round-trip test in `tests/test_metadata.py`.
- The **end-of-run server invite** (S4) originally scoped alongside these features was dropped from this release: `api_create_invite` belongs to the in-progress probe-and-invites feature, so invite generation will ship there rather than be duplicated here.

## [2.2.12] - 2026-06-07

### Security

- **DCE checksum verification now hard-fails on an unpinned platform (closes #37, phase 2)**. `_verify_dce_checksum` (`exporter/manager.py`) previously returned silently when no SHA-256 hash was pinned for the requested version/platform — the exact silent-skip behavior that left `osx-arm64` and `linux-arm64` downloading **unverified** DCE binaries for years (phase 1, v2.1.5, pinned the missing ARM hashes; this phase closes the hole structurally). It now raises `DCENotFoundError` naming the platform, refusing to use an unverified binary, and pointing at the escape hatches (`--skip-dce-verify` CLI / `skip_verify=True` API) plus a request to file a bug to add the hash. **Behavior change (latent breaking):** if a future contributor adds a platform to `_PLATFORM_MAP` without pinning its hash, Ferry now errors on that platform until the hash is added — instead of silently shipping an unverified binary. All five currently-supported platforms (`win-x64`, `linux-x64`, `osx-x64`, `osx-arm64`, `linux-arm64`) are pinned, so **no supported user is affected**; the existing `test_dce_checksums_json_covers_all_supported_platforms` regression guard keeps it that way. The missing-*checksums-file* path remains a skip (the file is bundled via `importlib.resources`; its absence is a packaging edge, not a platform-coverage gap). Locked by three tests in `tests/test_exporter_manager.py`: `test_dce_checksum_empty_hash_raises`, `test_dce_checksum_missing_version_raises`, and `test_dce_checksum_unpinned_platform_raises` (synthetic `win-arm64`). Deferred since v2.1.5 (PR #42) for soak time, now satisfied.
## [2.2.11] - 2026-06-07

### Security

- **Upgraded `aiohttp` 3.13.5 → 3.14.0 (closes Dependabot #22, #23)**. Two medium-severity advisories affect every `aiohttp < 3.14.0`: GHSA-jg22-mg44-37j8 (deserialization of untrusted data) and GHSA-hg6j-4rv6-33pg (cross-origin redirect leaks per-request cookies). Ferry ships `aiohttp` inside the PyInstaller binary and uses it for all Stoat/Autumn/Discord HTTP, so the patched runtime ships to users. The Dependabot bump (PR #58) was correct but could not merge on its own — see the test-harness fix below.
- **Upgraded `starlette` 1.0.0 → 1.2.1 (closes Dependabot #24)**. GHSA-86qp-5c8j-p5mr (medium): `starlette ≤ 1.0.0` is missing Host-header validation, which poisons `request.url.path` and can bypass path-based security checks. `starlette` is a transitive dependency (via `nicegui` and `fastapi`); both declare it without a version constraint, so the upgrade resolved cleanly to the latest `1.2.1` — past the `1.0.1` patch floor — with no other package churn in `uv.lock`. Full suite passes on the upgraded resolve.

### Bug Fixes

- **Test harness: aiohttp 3.14 / aioresponses 0.7.8 compatibility shim (`tests/conftest.py`)**. aiohttp 3.14 made `stream_writer` a *required* keyword-only argument of `ClientResponse.__init__`. aioresponses 0.7.8 builds its mock responses by calling that constructor directly without it, so under 3.14 every mocked HTTP call raised `TypeError: ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'` at `aioresponses/core.py:172` — collapsing the suite from 925 green to a mass failure (`test_api`, `test_autumn`, `test_connect`, `test_discord_client`, `provisioning/*` all red). This was purely a *mock*-construction incompatibility; Ferry's own runtime use of aiohttp 3.14 is unaffected. The upstream fix ([aioresponses#288](https://github.com/pnuckowski/aioresponses/pull/288)) is unmerged/unreleased (latest release is 0.7.8), so `conftest.py` carries an equivalent: a `setdefault("stream_writer", Mock(output_size=0))` wrapper around `ClientResponse.__init__`, guarded by an `inspect.signature` check so it is a no-op on `aiohttp < 3.14` and inert once aioresponses ships the fix. A `REMOVE THIS` marker ties the shim to the upstream PR for clean removal. Full suite restored to **925 passed**.

## [2.2.10] - 2026-05-19

### Security

- **`state.errors.append` callsites now sanitize messages through the token store (closes #47)**. v2.0.1 wired `SecureTokenStore` and added `_safe_error` / `sanitize_for_display` helpers, but a coverage audit during v2.2.0's ship review found five `state.errors.append` callsites that interpolated raw exception `repr()`s without going through any sanitizer: `migrator/emoji.py:296`, `migrator/reactions.py:112`, `migrator/pins.py:83`, `migrator/messages.py:382`, and `core/engine.py:599`. If a Stoat/Discord/Autumn aiohttp exception's `repr()` ever contained a token value (token-in-URL leak from a misconfigured HTTP library, 401 body echo, etc.) it landed in `state.json` unredacted — visible via `cat state.json`, `ferry stats`'s `last_error` 80-char preview (added in v2.2.3), and `reporter.generate_markdown_report`'s `### Errors` section. The exposure pre-dated `ferry stats`; that command surfaced it more discoverably without creating new ingress. Project rule `.claude/rules/security.md` ("Never log tokens... in log output, error messages, or state files") was violated by construction at all five sites.
- **Fix**: promoted `_safe_error` from `migrator/messages.py` (private, used at one site) to `core/security.py:safe_sanitize(token_store, text)` — a `None`-tolerant wrapper around `SecureTokenStore.sanitize` so test paths without a registered store don't have to thread a fake one through. All five callsites now wrap their `"message"` (or `"error"`) field through `safe_sanitize(config.token_store, ...)`. The `core/engine.py` site is the trickiest: the same `str(e)` interpolation also flowed into the wrapped `MigrationError`, the `MigrationEvent.message`, and the event `detail`'s `error` — all four are now sanitized via a single `safe_exc` local, since partial sanitization would still leak via the event log panel or the propagated exception.
- **Locked by** three regression tests in `tests/test_security.py`: `test_safe_sanitize_returns_text_unchanged_when_store_is_none` (no-op without store, so test paths keep working), `test_safe_sanitize_masks_registered_tokens` (multi-token replacement), and `test_safe_sanitize_at_state_errors_call_site` (end-to-end: synthetic token in a `RuntimeError`'s URL → masked in the resulting `state.errors[-1]["message"]`).
- **Acceptance**: `grep -n "state.errors.append" src/` shows all five sites; manual audit confirms each goes through `safe_sanitize` (4 migrator sites directly in the `"message"` value, 1 engine site via the shared `safe_exc` local). *Note: v2.2.3's CHANGELOG cross-referenced this fix as "#46" — the actual issue number is #47; #46 was the unrelated reporter fidelity bug fixed in v2.2.9.*

## [2.2.9] - 2026-05-19

### Bug Fixes

- **`reporter` fidelity score: reactions sub-score no longer inflates in partial state (closes #46)**. `generate_report` (`reporter.py:120`) and `generate_markdown_report` (`reporter.py:351`) both passed `reactions_total=len(state.pending_reactions)` to `compute_fidelity_score` — a denominator that's wrong in both terminal states. **Completed migration**: `pending_reactions` is drained → denominator is 0 → the function's `if reactions_total else 1.0` short-circuit silently returns 100%, hiding any reactions that failed. **Partial migration** (e.g. `reactions_applied=10`, `pending_reactions=[5 items]`): denominator is 5 → ratio is `10/5 = 200%`, inflating the overall score by up to 10 percentage points (the reactions weight). Both callsites now use the formula `state.reactions_applied + len(state.pending_reactions)`, matching what `stats.summarize_state` (added in v2.2.3) already does — so `ferry stats` and `migration_report.json`/`.md` agree on the reactions sub-score for the same state. Locked by `test_fidelity_reactions_partial_state_not_inflated` (asserts 60-70% for the 10/15 case) and `test_fidelity_reactions_completed_state_is_100_percent` (asserts the completed case now reaches 100% via the corrected formula, not the zero-denominator short-circuit). *Note: v2.2.3's CHANGELOG cross-referenced this fix as "#47" — the actual issue number is #46; #47 tracks the unrelated `state.errors` sanitization gap.*

## [2.2.8] - 2026-05-18

### Bug Fixes

- **`diff()` thread-keying disambiguates threads from forum posts** (`tests/provisioning/_applier.py`). Both Discord threads and forum posts use channel type `11`, so the previous `actual_threads = {ch.name: ch for ch in actual.channels if ch.type == 11}` would silently collide if a manifest grew to include a thread and a forum post with the same name — only one would survive in the dict and the other manifest entity would be spuriously reported as missing/needing creation. Re-keyed to `(parent_id, name)` tuples and threaded a `manifest_tc_id_to_discord_id` map through the diff so the lookup targets the correct text-channel parent. Current fixture (`Cool Thread` vs `Bug Report`) doesn't trigger the collision; the new `test_diff_distinguishes_thread_from_forum_post_with_same_name` regression test injects a colliding foreign forum post named "Cool Thread" inside `feedback-forum` and asserts the manifest's `Cool Thread` thread under `general` is still matched without drift.

### Docs

- **`tests/provisioning/README.md` "no CI" rule scoped explicitly**. The previous wording ("this script must NEVER run in CI") was ambiguous about whether the rule covered `provision_test_server.py` (yes — that's the human-only CLI) or `tests/provisioning/test_*.py` (no — those are hermetic via `aioresponses` and run alongside the rest of the suite in CI). Clarified that the rule applies to the CLI only.

## [2.2.7] - 2026-05-18

### Bug Fixes

- **Null-field collapse extended across the remaining parser call sites.** v2.2.6 (PR #51) fixed the `str(raw.get(K, default))` → `"None"` pattern in `_parse_channel` and `_parse_message`'s reference block — the four fields exposed by the new captured fixtures. The same pattern remained in seven other parsing sites (`_parse_export` `exportedAt`, `_parse_guild` `iconUrl`, `_parse_author` `discriminator`/`nickname`/`avatarUrl`, the inline `DCEEmoji` construction in `_parse_reaction` for `id`/`name`/`imageUrl`). Each of those produced the truthy 4-char string `"None"` when DCE serialized a present-but-null value — Pomelo-era users with no discriminator/nickname/avatar, guilds with no icon, and unicode emojis with no ID or image URL. Fixed by switching every site to the `str(raw.get(K) or default)` pattern, preserving the existing defaults (`""` everywhere except `discriminator`'s `"0000"`). Locked by three new unit tests against the private parse functions: `test_parse_guild_null_icon_url_collapses`, `test_parse_author_null_fields_collapse_to_defaults`, `test_parse_reaction_unicode_emoji_null_id` (the last verifies the unicode-emoji case end-to-end since unicode emojis natively emit `null` for both `id` and `imageUrl`).

## [2.2.6] - 2026-05-18

### Tests

- **Real DCE 2.47.1 fixtures replace synthetic ones (closes issue #35)**. Three real `DiscordChatExporter exportguild` captures from a Discord guild provisioned via `tests/provisioning/provision_test_server.py` now back the parser test contract: `Discord Ferry Test - general [1506019498094891120].json` (10 user messages + 1 `ThreadCreated`, including the 3-inline + 2-non-inline embed), `Discord Ferry Test - general - Cool Thread [1506019505778987190].json` (thread starter + reply), and `Discord Ferry Test - feedback-forum - Bug Report [1506019530294562938].json` (forum post body). The synthetic `Test Server - general - Cool Thread [888888888888888888].json` and `Test Server - Feedback Forum - Bug Report [999999999999999999].json` are removed. `test_parser.py` updated for the new filenames, the `parse_export_directory` count assertion (5 → 6 valid DCE JSONs), and the Discord-normalized `feedback-forum` parent name in `test_forum_export_detected`.

### Bug Fixes

- **Parser accepts DCE 2.47.1's enum-named channel types**. DCE 2.47.1 emits `"type": "GuildTextChat"` and `"type": "GuildPublicThread"` where pre-2.47 releases emitted integers (`0` and `11`). The parser previously called `int(raw["type"])` directly, which `ValueError`s on the new format — discovered when the real DCE captures used to replace `tests/fixtures/` synthetics failed to parse. Fixed with a `_DCE_CHANNEL_TYPE_TO_INT` mapping covering the 11 channel enum names emitted by DCE's `ChannelKind` and a `_coerce_channel_type` helper that accepts both string and integer inputs; downstream callers (`migrator/structure.py`, `migrator/messages.py`, `review.py`) continue to branch on Discord-canonical integer codes unchanged. Locked by five new unit tests covering int passthrough, known-string mapping, digit-only strings, `None` input, and unknown-string `ValueError`.
- **Null JSON fields collapse to `""` instead of the truthy string `"None"`**. DCE emits `null` for `categoryId`/`category` on top-level channels and for `topic` on threads — and for `reference.messageId` on `ThreadCreated` system messages. The previous `str(raw.get(K, ""))` pattern returned `"None"` for these (because `dict.get` only uses the default when the key is *missing*, not when it's present with a null value). The four-character truthy string `"None"` then slipped past downstream truthy guards in `migrator/structure.py:496` (`if cat_id and ...`), `review.py:73` (`if export.channel.category_id: ...`), and `cli.py`/`gui.py` channel-scan summaries — potentially leading to phantom-category creation. The synthetic fixtures hid the bug by using `""` empty strings; the real captures surfaced it. Fixed with `str(raw.get(K) or "")` in `_parse_channel` (3 fields) and `_parse_message`'s reference block (3 fields). Locked by `test_null_json_fields_collapse_to_empty_string` against the new captured fixtures.

## [2.2.5] - 2026-05-18

### Tooling

- **`tests/provisioning/` test-server provisioning CLI shipped (issue #35 enabler)**. Standalone Click-based tool with `provision` / `teardown` / `verify` subcommands that hits Discord's REST API using a Bot token from `DISCORD_TEST_BOT_TOKEN`. Human-run only; the import firewall (`[tool.hatch.build.targets.wheel] packages = ["src/discord_ferry"]`) ensures the bot-auth write paths never ship in installable wheels. Architecture: three-layer (transport `_bot_api.py` → logic `_applier.py` → CLI `provision_test_server.py`). Reconciler uses a sealed `DiffOpT` discriminated union with `assert_never` exhaustiveness; per-op priority ordering ensures channels are created before messages/threads/posts depend on them. Verified end-to-end against a live Discord guild — the full six-step smoke test (dry-run → provision → verify-match → idempotent re-provision → teardown → verify-drift) passes cleanly. 61 hermetic unit tests run alongside the rest of the suite (mocked via aioresponses).

### Bug Fixes

- **`diff()` channel-name normalization** (caught by the live smoke test, not the mock-based unit tests). Discord lowercases and hyphenates channel names server-side, so a manifest entry `"Feedback Forum"` is stored as `"feedback-forum"`. The diff comparator previously matched by exact name and falsely reported the forum channel as missing. Fixed with `_normalize_channel_name` (lowercase + spaces→hyphens + strip non-`[a-z0-9_-]`) applied symmetrically on both sides of the lookup. Locked in by `test_diff_matches_discord_normalized_channel_names`.
- **403 error message no longer leaks guild IDs**. The previous `url.split('/')[-2]` returned the guild snowflake for nested endpoints like `/api/v10/guilds/{id}/channels`, exposing a guild ID in the exception text. Now uses `url.removeprefix(DISCORD_API_BASE)` to keep only the relative path.
- **`ProvisioningPermissionError` exits 2 across all CLI subcommands**. Previously a 403 fell through to the bare `ProvisioningError` handler and exited 1 (drift) instead of 2 (couldn't-determine), violating the documented 3-way exit-code contract in `tests/provisioning/README.md`. Permission errors are now handled explicitly alongside auth errors.
- **`reconcile_teardown` no longer swallows auth/permission failures into `skipped_count`**. Previously a token revocation mid-teardown reported "deleted 0, skipped N" with exit 0, masking the failure. The function now re-raises `ProvisioningAuthError` and `ProvisioningPermissionError` so the CLI can surface them as exit 2. Other `ProvisioningError`s (5xx, network) are still treated as transient and counted in `skipped_count`. The CLI now also prints a warning when `skipped_count > 0` so operators have a clear retry hint.

## [2.2.4] - 2026-05-17

### Internal

- **Foundation for `tests/provisioning/` dev tool (issue #35 enabler)**. Establishes the architectural firewall and bootstraps the package for the upcoming Discord test-server provisioning CLI. Three pieces shipped: (a) `[tool.hatch.build.targets.wheel] packages = ["src/discord_ferry"]` stanza in `pyproject.toml` ensures `tests/provisioning/` is excluded from built wheels (verified: 0 entries under `tests/` in the built `.whl`); (b) PEP 561 `src/discord_ferry/py.typed` marker so downstream `mypy tests/` can honor the package's types instead of treating it as untyped; (c) `tests/provisioning/__init__.py` + `_bot_api.py` exposing a `ProvisioningError` hierarchy that deliberately does NOT inherit from `FerryError` — the firewall is enforced structurally (location → wheel exclusion) and reinforced in types (no shared exception root). The full provisioning tool (provision/teardown/verify subcommands, ~1,400 LOC across ~20 commits) is planned for a follow-up session.

### Changed

- **Python floor formalized at 3.11.** `requires-python` bumped from `>=3.10` to `>=3.11`, the 3.10 trove classifier removed, ruff `target-version` bumped to `py311`, and mypy `python_version` bumped to `3.11`. CLAUDE.md has declared Python 3.11+ as the floor since the project's first internal commit; this PR aligns the externally-visible metadata. Any consumer pinning to Python 3.10 should remain on `2.2.3` or upgrade their interpreter. The ruff bump surfaced 12 modernization opportunities in `src/`, all auto-applied: `datetime.now(timezone.utc)` → `datetime.now(UTC)` (PEP 615 alias added in 3.11), and `except asyncio.TimeoutError` → `except TimeoutError` (3.11 stdlib alias). Behavioural equivalence preserved.

## [2.2.3] - 2026-05-16

### Features

- **`ferry stats <output-dir>` CLI subcommand**: post-migration introspection that reads `state.json` + `message_map.json` from a completed (or in-progress) migration and prints a Rich table to the console — entity counts (channels/roles/categories/emojis/messages), message counters (attachments/pins/reactions/replies/embeds/failed/prior), fidelity score (overall + 5 sub-scores via `reporter.compute_fidelity_score`), error/warning summary with truncated last-message preview, and elapsed duration. Optional sub-sections render when state contains them: per-channel message breakdown (top 20 by count + "+N more"), rollback counters (when `state.rollback_progress` is set), and a `[DRY-RUN]` badge in the title (when `state.is_dry_run` is true). Pure state-only introspection — no Stoat API, no Autumn, no Discord API, no DCE re-parse. Failure modes are clean: missing state.json or corrupt JSON exits 1 with a single human-readable error line (no traceback).
- **Zero-denominator fidelity handling**: when an empty category (e.g. zero embeds in the migration) would otherwise short-circuit `compute_fidelity_score` to 100%, the sub-score renders as `n/a` instead. The `overall` score still uses the function's arithmetic unchanged ("no embeds to migrate = no embed loss" is a defensible interpretation).
- **`reactions_total` derivation**: `MigrationState` lacks a `reactions_total` field, so `summarize_state` derives it as `reactions_applied + len(pending_reactions)` — yielding correct ratios in both completed and partial states. Note: `reporter.generate_report` uses the existing `len(pending_reactions)` formula, which overestimates in partial state; the two surfaces will report different reaction sub-scores until reporter is aligned in a follow-up (tracked in #47).

### Internal

- **`reporter._calculate_duration` → `reporter.calculate_duration`**: promoted from private to public (with docstring) so `stats.py` and `reporter.py` share one implementation of the ISO-8601 elapsed-seconds helper. No behavioural change; pure rename + docstring + return-literal-tightening (`0` → `0.0`) for mypy strict mode.

### Known Issues

- **Unsanitized error messages in `state.errors` (pre-existing, tracked in #46)**: audit during 2.2.0 ship-review found that `state.errors.append` callsites in `migrator/emoji.py:296`, `migrator/reactions.py:112`, `migrator/pins.py:83`, `migrator/messages.py:382`, and `core/engine.py:599` do **not** wrap their messages with `_safe_error` / `config.token_store.sanitize` — meaning a Stoat/Discord/Autumn exception whose `repr()` happens to contain a token (e.g. token-in-URL leak from a misconfigured HTTP library) would land in `state.json` unredacted. This pre-existing exposure also affects `reporter.generate_markdown_report` (which renders `state.errors` verbatim) and on-disk inspection of `state.json` itself. The new `ferry stats` command surfaces these errors via a truncated 80-char preview, making the exposure more discoverable but not creating new ingress. **Follow-up**: #46 will wrap all `state.errors.append` callsites in the migrator with `_safe_error`. The fix belongs in the engine, not in stats consumers — stats was deliberately designed without `FerryConfig`/`token_store` access. Until then, treat `ferry-output/state.json` as sensitive (it always has been).

## [2.2.2] - 2026-05-16

### Added

- **Adaptive heartbeat during prolonged DCE silence** (#39). Long Discord channel-enumeration phases (5-15 min on large guilds) previously left the GUI silent because Spectre.Console's status ticker is suppressed when stdout is piped. The runner now spawns a `_heartbeat` task alongside the existing stdout/stderr drain that emits `status="heartbeat"` `MigrationEvent`s at adaptive intervals (60s, 120s, 240s, then capped at 300s). Any "real activity" (per-channel progress, phase headlines, success, or any non-banner/non-status-dot line) resets the interval to 60s. Heartbeats appear in the GUI log panel only — the progress bar and channel label are untouched. `MigrationEvent.status` now recognizes `"heartbeat"` as a value; existing consumers that don't recognize it degrade gracefully (the GUI's `on_export_event` already pushes every event to the log regardless of status).

## [2.2.1] - 2026-05-16

### Added

- **DCE contract test in CI** (#34). New `tests/test_dce_contract.py` invokes the real DCE 2.47.1 `--help` and asserts every flag `_build_dce_command()` passes is still present in DCE's output; new `tests/test_dce_output_replay.py` replays the captured fixture through `parse_dce_line` and asserts the typed result for each non-comment line. Both run in a dedicated `contract-test` CI job on `ubuntu-latest` (Python 3.12, .NET 8 preinstalled on `ubuntu-24.04`), with the DCE binary cached across runs by `DCE_VERSION`. When `DCE_VERSION` bumps in future, the cache key changes and a fresh DCE is downloaded + re-asserted against `_build_dce_command()` — drift in either direction now fails CI on the bump PR instead of silently shipping.

## [2.2.0] - 2026-05-16

### Changed

- **`MigrationEvent.current` / `MigrationEvent.total` semantics changed for export-phase events** (issue #23). Previously these fields held the per-channel percentage (`current=NN, total=100`). They now hold the overall channel count (`current=channels_done, total=total_channels`). The progress-bar fraction `current / total` is unchanged in shape (still 0-1) but its meaning is "fraction of channels completed" instead of "fraction of current channel completed." Any external consumer of `MigrationEvent` reading these fields needs to know.

### Fixed

- **DCE export progress was invisible -- bug latent since 2026-02-28** (issue #23): the per-channel progress regex never matched any line that DCE 2.47.1 actually emits. Verified by reading `Tyrrrz/DiscordChatExporter@2.47.1` source -- DCE uses Spectre.Console's `FallbackProgressRenderer` when stdout is piped, which emits `<channel name>: NN%` (no brackets, no `#`, hierarchical names joined with ` / `). User-visible symptom: GUI sat on `"Discord Chat Exporter -- Started"` for 5-30 minutes on large servers. Replaced with a typed-union parser (`src/discord_ferry/exporter/dce_output.py`) handling all DCE 2.47.1 line types: `PerChannel`, `Phase` (Fetching/Fetched/Exporting headlines), `Success`, `Banner`, `StatusDot`, and `Raw` fallthrough for unknown lines. Unknown lines now surface to the GUI as `[dce] <line>` instead of being lost to `logger.debug`. Reported by @The-Red-Priest.
- **Windows console flash during DCE startup**: DCE child process was spawned without `CREATE_NO_WINDOW`, briefly flashing a console window before the parent claimed the pipes. Now spawned with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows.
- **Stderr drain task leaked on cancel path**: cancelling an export mid-stream left the `_read_stderr` task running until garbage collection. Now cleaned up via `try/finally` on every exit path.
- **`asyncio.LimitOverrunError` would crash the GUI on long lines**: a single DCE stdout line exceeding 64 KiB (improbable, but possible from malformed JSON paths in error traces) would bubble out of the stdout loop, bypassing cancel checks and crashing the event loop. Now caught and reported as `[dce] <truncated N bytes; line exceeded 64 KiB>` while the loop continues.
- **Windows cancel left partial JSON files**: hard-kill on Windows gave DCE no chance to flush. Now sends `CTRL_BREAK_EVENT` to the DCE process group (DCE has a `CancelKeyPress` handler), with a 3-second graceful-shutdown window before falling back to hard kill. POSIX behavior unchanged (already used `SIGTERM`). Caveat: the channel currently being exported may still produce a partial JSON file -- users should expect to delete the export folder before retrying after a cancel.

## [2.1.6] - 2026-05-16

### Fixed

- **Open Report button now works on Windows** (#38). Previously, the GUI's "Open report" action tried to invoke `xdg-open` on Windows (`sys.platform == "win32"` falls into the non-darwin branch), which doesn't exist — the click silently failed with a `ui.notify` toast. Replaced with a three-branch platform check using `os.startfile(path)` on Windows. Bundled audit confirmed `xdg-open` was the only Windows-incompat bug in `src/` (full grep results in `docs/superpowers/specs/2026-05-15-issue-38-windows-compat-audit-design.md`); no other code paths needed changes.

## [2.1.5] - 2026-05-16

### Fixed

- **ARM platforms (`osx-arm64`, `linux-arm64`) now have SHA-256 hash verification on DCE download** (#37, phase 1 of 2). Previously, `_verify_dce_checksum` (`src/discord_ferry/exporter/manager.py:75-77`) silently skipped verification for any platform without a pinned hash — Apple Silicon (default Mac since 2020) was downloading DCE binaries with no integrity check. Now pinned for DCE 2.47.1: `osx-arm64`, `linux-arm64`. The silent-skip behavior is preserved as a fallback for any future unrecognized platform (phase 2, shipping as v2.2.0, will flip silent-skip to a loud `DCENotFoundError`). New schema-validation tests in `tests/test_exporter_manager.py::TestDceChecksumsJson` guard against future regressions: any new platform added to `_PLATFORM_MAP` without a corresponding hash now fails CI.

## [2.1.4] - 2026-05-16

### Fixed

- **Embed fields with `isInline: true` now correctly render as pipe-separated rows in migrated messages** (#36). The parser previously read the wrong key (`inline`), causing every multi-field Discord embed to render as a stacked list since v1. Verified against DCE 2.47.1 source (`JsonMessageWriter.cs:259` writes `isInline`). Re-running migration on a server with bot-posted embeds will produce visibly cleaner output. The accompanying parser-audit appendix (see spec `docs/superpowers/specs/2026-05-15-issue-36-isinline-and-parser-audit-design.md`) confirmed this was the only key-typo in `transforms.py` and `dce_parser.py`.

## [2.1.3] - 2026-05-14

### Fixed

- **GITHUB_TOKEN cascade gap closed (Option C)**: `auto-tag.yml` now uses a GitHub App installation token (minted via `actions/create-github-app-token@v3`) instead of the default `GITHUB_TOKEN` for tag pushes. The default token by GitHub safety does NOT trigger downstream workflows — three sequential releases (v2.1.0, v2.1.1, v2.1.2) all required a manual `git push --delete origin v<tag>` + re-push protocol to fire `release.yml`. After this fix, `v*.*.*` tag pushes from `auto-tag.yml` fire `release.yml` automatically. Credentials live in the new `auto-tag` GitHub Environment (`AUTO_TAG_APP_CLIENT_ID` variable + `AUTO_TAG_APP_PRIVATE_KEY` secret). The App (`Ferry Auto-Tag Bot`, owned by `nordscope-fi`, installed on this repo only) has `Contents: Read and write` permission — minimum scope. No user-visible code changes.

## [2.1.2] - 2026-05-14

### Fixed

- **CI hygiene correction — `upload-artifact` and `download-artifact` were still on Node 20 in v2.1.1**: the v2.1.1 pins `actions/upload-artifact@v5` and `actions/download-artifact@v6` were chosen based on release-notes wording ("supports Node v24.x") that turned out to mean *compatible with Node 24 runners*, not *runs on Node 24 by default*. The action.yml's `runs.using:` field is authoritative — for `upload-artifact` that flipped from `node20` to `node24` at v6.0.0; for `download-artifact` at v7.0.0. GitHub's release.yml run for v2.1.1 surfaced this with a `Node.js 20 actions are deprecated` annotation. Bumped both pins to their actual lowest Node-24 majors: `upload-artifact@v5 → @v6`, `download-artifact@v6 → @v7`. All other v2.1.1 pins (`checkout@v6`, `setup-uv@v7`, `upload-pages-artifact@v5`, `deploy-pages@v5`, `action-gh-release@v3`) were verified against `action.yml` and are genuinely on Node 24.

## [2.1.1] - 2026-05-14

### Changed

- **CI hygiene — Node 20 → Node 24 Actions runtime**: bumped every Node-20 `actions/*` reference across all four workflows (`ci.yml`, `release.yml`, `auto-tag.yml`, `docs.yml`) to its lowest version that ships Node 24, ahead of GitHub's June 2026 runtime retirement. Specifically: `actions/checkout@v4 → @v6`, `actions/upload-artifact@v4 → @v5`, `actions/download-artifact@v4 → @v6`, `actions/upload-pages-artifact@v3 → @v5`, `actions/deploy-pages@v4 → @v5`, `astral-sh/setup-uv@v5/@v6 → @v7` (also fixes the version drift in `docs.yml`), `softprops/action-gh-release@v2 → @v3`. `pypa/gh-action-pypi-publish` unchanged (Docker action, unaffected). No user-visible code changes; pure CI maintenance.

## [2.1.0] - 2026-05-14

### Features
- **Rollback engine** (#10): new `ferry rollback --output-dir <path> [--yes] [--force-unlock]` CLI subcommand and a GUI "Rollback this migration" button on the migration-complete page. Reverses a recorded migration by deleting Ferry-created channels, roles, custom emoji, and Ferry-owned categories from the Stoat target server. Reads `state.json` for the entity IDs to delete. Idempotent re-runs: a 404 response is treated as "already deleted" via the new `expected_404_ok=True` path on `_api_request`. Supports partial-rollback resume via `state.rollback_progress.rolled_back_ids` (a new set field on `MigrationState`); the existing entity maps (`channel_map` / `role_map` / `emoji_map`) are **never** mutated by rollback (forensic preservation). Surfaces untracked-Ferry-suspect channels — channels present on the Stoat server but absent from `state.channel_map`, likely orphans from a crashed prior migration — in the confirmation gate for per-item opt-in. Channel deletes run concurrently (bounded by `--max-concurrent-requests`, default 5); roles and emoji are serialized on Stoat's shared `/servers` 5/10s bucket. Acquires the same `[FERRY_LOCK:...]` server-description marker as `run_migration`, so a concurrent rollback or migration cannot collide. Autumn-hosted attachments are **not** removed — no public DELETE endpoint exists for Autumn files (documented in known-limitations).

### Fixes
- **GUI progress emit gap during DCE export** (issue #23): the GUI used to freeze on `"Checking for DCE binary..."` for the entire DCE channel-enumeration phase, which on large servers can run several minutes before DCE prints its first per-channel progress line. Added three intermediate emits: `"Verifying .NET 8 runtime..."` (before `detect_dotnet()`), `"Launching DiscordChatExporter..."` (before the subprocess spawn), and `"DiscordChatExporter started — enumerating channels..."` (inside `run_dce_export` immediately after the subprocess is alive but before the first stdout line). Applied symmetrically to the GUI shell and the CLI/engine path.

## [2.0.2] - 2026-04-21

### Fixes
- **DCE version bump**: DiscordChatExporter upgraded from 2.46.1 to 2.47.1. The upstream 2.46.1 release is no longer the latest tag and Ferry's hardcoded download URL was returning 404 for new users (reported in discussion #8 and issue #7). SHA-256 checksums for all three platforms (win-x64, linux-x64, osx-x64) added to `dce_checksums.json`; 2.46.1 entries retained for rollback.
- **Broken Stoat app link**: `app.stoat.chat` is NXDOMAIN — canonical URL is `https://stoat.chat/app`. Fixed in README (#15, thanks @FedeltaMedia) and swept the remaining 3 references in `docs/index.md` and `docs/getting-started/setup-stoat.md`.

## [2.0.1] - 2026-03-19

### Fixes
- **Token sanitization wired** (S1): `SecureTokenStore` now created in engine and used at error output boundaries — token values stripped from `state.errors` and event messages
- **DCE checksums populated** (S10): SHA-256 hashes for DCE v2.46.1 (win-x64, linux-x64, osx-x64) — supply chain verification now active
- **GUI thread strategy** (S7): Thread strategy dropdown added to GUI setup page
- **Forum index edit** (S15): Re-runs edit existing forum index message via `api_edit_message` instead of creating duplicates
- **Fidelity scoring expanded** (S18): 5 categories (messages 40%, attachments 25%, embeds 15%, replies 10%, reactions 10%) — up from 2

## [2.0.0] - 2026-03-19

### Security
- **Token security hardening** (S1): `SecureTokenStore` for token masking, `repr=False` on tokens, NiceGUI binds to localhost, Stoat token cleared from storage
- **DCE binary verification** (S10): SHA-256 hash verification for DiscordChatExporter downloads

### Performance
- **Parallel message sends** (S4): Cross-channel parallelism via `asyncio.gather` with `ChannelResult` accumulators
- **Adaptive rate limiting** (S9): 429-frequency optimization with rolling window and auto-adjusting delay multiplier

### Features
- **Thread strategy** (S7): `--thread-strategy` flag with flatten/merge/archive modes
- **Message splitting** (S3): Messages >2000 chars split with `[continued K/N]` markers instead of truncation
- **Delta migration** (S19): `--incremental` flag for migrating only new messages since last run
- **Migration lock** (S17): Advisory lock via server description prevents concurrent migrations
- **Fidelity scoring** (S18): Quantified migration fidelity percentage in report

### Fixes
- **Resume correctness** (S2): `completed_channel_ids` set replaces fragile snowflake ordering
- **Emoji collisions** (S6): Duplicate sanitized names get `_2`, `_3` suffixes
- **Underline+bold** (S3): `****` collision collapsed to `**`
- **Cross-channel replies** (S8): Text fallback annotation instead of silent drop
- **Banner auth** (S11): Discord auth header for CDN downloads
- **Masquerade discriminator** (S11): Truncated names append author ID suffix
- **DCE freshness** (S11): Warn >7 days, error >30 days with `--force` override
- **Reaction counts** (S12): Native mode appends original count annotation
- **Embed overflow** (S3): Failed embeds reported with `[N embed(s) could not be migrated]`

### Infrastructure
- **Separate message_map.json** (S5): Reduces state.json size dramatically
- **Emoji in embeds** (S6): Discovery scans embed description, title, and field values
- **Upload verification** (S13): Optional `--verify-uploads` for post-upload size check
- **Forum index rebuild** (S15): Index built during REPORT phase with actual migration data
- **Orphan detection** (S16): `--cleanup-orphans` flag detects unreferenced Autumn uploads
- **Code signing** (S20): CI pipeline prepared for macOS/Windows binary signing

### Breaking Changes
- State format v2: `completed_channel_ids` replaces `last_completed_channel`/`last_completed_message`
- `message_map` stored in separate `message_map.json` file
- v1 state files automatically migrated on first load (backup created)

## [1.7.1] — 2026-03-18

### Added

- **Known limitations guide**: Centralized `docs/guides/known-limitations.md` listing every structural impossibility with what-Discord-has / what-Stoat-gets / workaround columns.
- **Pre-flight checklist**: `docs/guides/pre-flight-checklist.md` — 10-step preparation guide preventing common migration failures.
- **Forum post index channel**: Auto-generated `forum-index` channel per forum-derived category with pinned message listing all posts and message counts.

## [1.7.0] — 2026-03-18

### Added

- **Exponential backoff + circuit breaker**: API retries use `min(2^attempt, 60)` + jitter instead of fixed 2s. Circuit breaker opens after 5 consecutive non-429 failures (30s pause). asyncio.Semaphore bounds concurrent requests.
- **Discord link rewriting**: Jump links (`discord.com/channels/...`) rewritten to Stoat channel references. Invite links (`discord.gg/...`) annotated as expired. Covers all URL variants (canary, ptb, discordapp.com).
- **Edited message indicator**: Messages with `timestamp_edited` now show `*(edited)*` after the timestamp prefix.
- **Attachment overflow handling**: Messages with >5 attachments get text fallback listing skipped filenames instead of silent truncation.
- **Embed URL validation**: Expired Discord CDN embed media URLs (thumbnail, image) are detected and stripped, preserving text content.
- **Markdown migration report**: `migration_report.md` generated alongside JSON with human-readable summary table, errors, and warnings.
- **Server banner migration**: Banner hash extracted from Discord API, downloaded from CDN, uploaded to Autumn, applied via `api_edit_server`.

## [1.6.0] — 2026-03-18

### Added

- **Dead-letter queue**: Failed messages tracked as typed `FailedMessage` objects with Discord ID, error, and content preview. New `run_retry_failed()` re-processes failures using single-scan strategy.
- **Configurable reaction strategy**: New `reaction_mode` config — `"text"` (default) appends `[Reactions: emoji count]` to content (zero extra API calls), `"native"` keeps Phase 9 behavior, `"skip"` ignores reactions entirely.
- **Per-member permission override warnings**: User overrides (type=1) now counted per channel, surfaced in pre-migration review and report with workaround suggestion ("create single-user roles").
- **Inline embed field layout**: Embed fields with `inline=True` grouped into rows with `|` separators (max 3 per row). Non-inline fields render on their own lines.
- **Orphaned Autumn asset tracking**: Every upload tracked; after successful send, IDs marked as referenced. Post-migration report shows unreferenced file count.
- **Thread filtering by message count**: New `min_thread_messages` config (default 0) excludes threads below the threshold. Filtered threads logged as warnings.
- **Post-migration validation**: Optional `validate_after` phase compares Stoat server channel/role counts against state maps via `api_fetch_server()`. Reports discrepancies.

## [1.5.0] — 2026-03-18

### Added

- **Avatar pre-flight phase**: New migration phase uploads all unique author avatars to Autumn before message migration, preventing broken masquerade avatars when Discord CDN URLs expire.
- **CDN URL expiration detection**: Validates Discord CDN signed URLs during export validation and warns when attachment URLs have expired, with recommendation to re-export with `--media`.
- **Configurable checkpoint interval**: New `checkpoint_interval` config field (default: 50) controls how often migration state is saved, with a 5-second time throttle to prevent I/O thrashing.
- **Timestamp preservation guide**: New `docs/guides/timestamps.md` documenting why message timestamps change and the self-hosted MongoDB workaround.
- Regression tests for audit-verified features (emoji phase ordering, ADMINISTRATOR permission mapping, deny-bit pipeline).

### Fixed

- **Security**: ADMINISTRATOR bit in deny context no longer incorrectly expands to ALL permissions. Other deny bits alongside ADMINISTRATOR are now correctly translated.
- **Security**: Missing Discord token warning upgraded from `status="progress"` to `status="warning"` with explicit mention that private channels may become publicly visible.
- **Resilience**: HTTP 413 from Autumn now produces a specific "File too large" error message with file size and limit, instead of a generic upload failure.
- **Resilience**: Oversized attachments are pre-checked against size limits before upload attempt, with text placeholder injected into message content.
- **Resilience**: Expired CDN URLs produce `[Attachment expired: filename]` placeholder in message content instead of silent failure.

## [1.4.0] — 2026-03-09

### Fixed

- **Category creation endpoint**: Replaced non-existent `POST /servers/{id}/categories` and `PATCH /servers/{id}/categories/{id}` with correct `PATCH /servers/{id}` using the server's `categories` array property. Categories are now built locally with client-generated IDs and sent in a single PATCH call.
- **Emoji creation endpoint**: Replaced non-existent `POST /servers/{id}/emojis` with correct `PUT /custom/emoji/{autumn_id}` using `parent` object (`{"type": "Server", "id": server_id}`). The Autumn file ID is now the emoji's permanent Stoat ID.
- **Channel name truncation**: Reduced from 64 to 32 characters to match Stoat API `maxLength` constraint.
- **Message nonce deprecated**: Replaced `nonce` body field with `Idempotency-Key` HTTP header for message deduplication. Resume logic unaffected (keyed by Discord message ID).

### Added

- **String sanitization module** (`migrator/sanitize.py`): `truncate_name()` (generic 32-char truncation) and `sanitize_emoji_name()` (lowercase, `[a-z0-9_]` only, 32-char max, fallback to `"emoji"`).
- **Role name truncation**: Role names truncated to 32 characters before API call.
- **Category title truncation**: Category titles truncated to 32 characters.
- **Masquerade name truncation**: Display names truncated to 32 characters.
- **Emoji name sanitization**: Custom emoji names sanitized to `^[a-z0-9_]+$` pattern and 32-character limit.
- **`extra_headers` support**: `_api_request()` now accepts optional extra HTTP headers (used for `Idempotency-Key`).
- **14 new tests**: sanitize helpers (12), masquerade truncation (1), role name truncation (1) — 440 total passing.

### Changed

- **`api_create_emoji()` signature**: Now takes `emoji_id` (Autumn file ID), `name`, and `server_id` instead of `name` and `parent` (Autumn ID).
- **`api_send_message()` signature**: `nonce` parameter replaced with `idempotency_key`.
- **`api_create_category()` and `api_edit_category()` removed**: Replaced by `api_upsert_categories()`.
- **Category management rewrite**: `run_categories()` generates category IDs client-side and sends a single PATCH. `run_channels()` rebuilds the categories array for channel assignment without a server fetch.
- **`cli.py` build command**: Updated to use `api_upsert_categories()` with client-generated IDs.
- **Documentation**: Updated `stoat-api-notes.md` with correct endpoints, string limits, and deprecation notes.

## [1.3.0] — 2026-03-01

### Added

- **Discord permission migration**: Fetches guild roles and channels via Discord REST API, translates permission bitfields from Discord bit space to Stoat bit space, and applies role permissions during Phase 4 (ROLES). ADMINISTRATOR expands to all individual Stoat permissions.
- **Channel permission overrides**: Per-role and @everyone channel overrides fetched from Discord API and applied during Phase 6 (CHANNELS).
- **NSFW flag migration**: Channel NSFW status fetched from Discord API and set during channel creation.
- **Pre-creation review**: Blocking confirmation step shows summary (roles, channels, categories, emoji, messages) and warnings before creating anything on Stoat. GUI shows dialog; CLI shows Rich table.
- **Post-migration checklist**: Enhanced report includes actionable next steps (verify channels, review permissions, check emoji, invite members).
- **Server blueprint export/import**: `ferry export-blueprint` converts a DCE export directory into a reusable JSON blueprint. `ferry build` creates a Stoat server from a blueprint or preset template.
- **3 preset server templates**: Gaming, Community, and Education — each with roles, permissions, categories, and channels.
- **Discord metadata persistence**: `discord_metadata.json` stores translated permissions and NSFW flags alongside `state.json` for resume support.
- **4 new Stoat API functions**: `api_set_role_permissions`, `api_set_server_default_permissions`, `api_set_channel_role_permissions`, `api_set_channel_default_permissions`.
- **64 new tests**: permissions (10), metadata (5), Discord client (6), API (4), structure (8), engine (4), review (10), reporter (4), blueprint (9), CLI (3), GUI (2) — 426 total passing.

### Fixed

- Remove hardcoded local path from `.claude/settings.json` for open-source readiness.
- Update README "How It Works" to reflect 1-Click Migration as the default workflow.
- Update GUI walkthrough: rewrite Setup screen for two-mode layout, add Export screen docs, fix phase count (11 → 12).
- Fix stale "11 phases" references in first-migration guide and brief.
- Add "What is Stoat?" section to README with signup and setup links.
- Explain what credentials are needed and where to find them in README Step 1.
- Replace jargon "masquerade" with plain language in all user-facing docs.
- Fix "Stoat bot token" → "Stoat user token" in first-migration prerequisites.
- Fix channel @everyone permission overrides silently dropped during migration.
- Fix `ferry build` NameError when printing completion message.
- Fix PyInstaller binary missing template JSON files.

## [1.2.1] — 2026-03-01

### Added

- **CLI ToS disclaimer**: Orchestrated mode now prompts for Discord ToS acknowledgment. Use `--yes` / `-y` to skip in scripts.
- **GUI smart resume**: Export page detects cached exports and offers [Use Cached] or [Re-export] choice.
- **DCE download retry**: `download_dce()` retries once on network error before failing.
- **Built-in token help**: "How to find these?" opens an inline dialog with step-by-step instructions instead of linking to external wiki.

## [1.2.0] — 2026-02-28

### Added

- **Phase 0 — DCE Orchestration**: Ferry can now download and run DiscordChatExporter automatically. Users provide a Discord token and server ID instead of manually exporting. Existing offline mode (`--export-dir`) still works.
- **Streaming JSON parser**: `stream_messages()` uses ijson to parse messages one at a time, keeping memory usage flat for large exports
- **`metadata_only` parsing mode**: `parse_export_directory(metadata_only=True)` skips message loading for fast validation
- **CLI orchestrated mode**: `--discord-token` + `--discord-server` flags for automatic export, mutual exclusion with `--export-dir`
- **GUI orchestrated mode**: Mode toggle (Orchestrated / Offline), Discord credential inputs, ToS checkbox, `/export` page with progress bar
- **Single-pass author name collection**: `validate_export()` now collects author names during validation, eliminating a second full scan
- **New dependency**: `ijson>=3.0` for streaming JSON parsing
- **49 new tests**: streaming parser, metadata_only, CLI orchestrated mode, GUI phase labels — 355 total passing

### Changed

- **Engine phases**: Now 12 phases (EXPORT through REPORT); validate uses `metadata_only=True`
- **All message-consuming phases** (messages, emoji, roles) use `stream_messages()` when exports were parsed with `metadata_only=True`
- **Runner lifecycle events**: Engine owns started/completed events; runner only emits progress
- **Discord token security**: GUI clears token from persistent storage in `finally` block (covers failure paths)
- **`validate_discord_token`**: Now catches `aiohttp.ClientError` for network failures
- **Documentation rewrite**: 5 docs pages updated to show orchestrated mode as primary workflow

### Fixed

- **mypy webview error**: Added `type: ignore[import-not-found]` for optional `webview` import

## [1.1.0] — 2026-02-28

### Changed

- **GUI setup page redesign**: Step indicator wizard (Configure → Validate → Migrate → Done), pre-flight checklist banner with prerequisite links, dark navy header, IBM Plex Sans font, fade-in animations
- **Hosted/self-hosted toggle**: Replaces bare "Stoat API URL" input with a toggle defaulting to Official Stoat; self-hosted URL field appears only when needed
- **Inline browse button**: Folder picker icon moved inside the export path input field
- **Amber action buttons**: Primary actions use amber-700 instead of blue-600
- **State restoration**: All setup fields persist across back-navigation from validate page
- **URL scheme validation**: Self-hosted URLs must start with http:// or https://
- **Step indicators on all pages**: Validate and migrate screens show progress through the wizard

## [1.0.1] — 2026-02-28

### Fixed

- **FERRY_MIN_PERMISSIONS was wrong since v1.0.0**: Bits 24/25 were set instead of bit 20 (ViewChannel). Rewritten as bitwise OR expression for clarity. Added ManageCustomisation (bit 4) for emoji creation. Correct value: 1,022,361,624. **If you migrated with v1.0.0, grant ViewChannel and ManageCustomisation permissions manually on servers created by Ferry.**
- **Forum/media channel headers**: Thread types 15 (GUILD_FORUM) and 16 (GUILD_MEDIA) now get "[Forum post migrated from #parent]" instead of generic "[Thread migrated from #parent]"
- **CLI validate ETA**: Now respects `--rate-limit` option instead of hardcoding 1.0s
- **Suppressed warnings notice**: Non-verbose CLI migration now prints "{N} warning(s) suppressed — run with -v to see details"
- **Specific field validation errors**: GUI setup page names which fields are missing instead of generic "all required"
- **~10 docs inaccuracies**: wrong state filename, stale GitHub URL, missing mypy in verification, DMG→ZIP for macOS, false "interactive prompt" claim, missing --rate-limit docs, missing FERRY_STORAGE_SECRET docs

### Added

- **Structured warning/error types**: All `state.warnings` and `state.errors` dicts now include a `"type"` field for downstream filtering
- **GUI folder picker**: Browse button using pywebview native folder dialog (disabled gracefully without pywebview)
- **GUI server name input**: Optional server name in Advanced Options
- **Accessible phase chips**: Text indicators (✓ ● ✗ — ⚠) on phase chips so status is not conveyed by color alone (WCAG 1.4.1)
- **8 new tests**: API retry (network error, 502/503), state backward compat, embed/sticker/poll e2e — 306 total passing

## [1.0.0] — 2026-02-28

First stable release. All 11 migration phases implemented with 298 passing tests.

### Added

- **Rich CLI progress bars**: Live dashboard with phase progress bar, per-channel message progress bar with ETA, and running stats (messages/errors/warnings)
- **Poll migration**: DCE poll data parsed and flattened into message content as formatted text
- **Sticker image upload**: Locally downloaded sticker images uploaded as message attachments (text fallback for missing/Lottie stickers)
- **Embed media upload**: Embed thumbnails and images from local `--media` exports uploaded to Autumn and attached to embeds
- **Forum categories**: Forum/media channel threads (type 15/16) grouped into dedicated categories named after the parent forum
- **Role rank ordering**: Best-effort second pass sets role rank from DCE position data after creation
- **Permission pre-check**: CONNECT phase verifies server accessibility when using `--server-id` (best-effort, non-fatal)
- **`skip_threads` GUI checkbox**: Exposed in Advanced Options alongside existing skip toggles
- **`attachments_uploaded` counter**: Accurate attachment count in state and reports (replaces deduplicated cache length)
- **GitHub Actions docs workflow**: `docs.yml` builds and deploys MkDocs Material site to GitHub Pages on push to main
- **SVG project logo** and asset build instructions for platform icons
- **12 new tests** covering polls, stickers, embeds, forum categories, role rank, permission pre-check — 298 total passing

### Fixed

- **`completed_at` timing**: Report timestamp now set before `generate_report()` runs, giving correct duration
- **`silent` messages**: All migrated messages sent with `silent: true` to prevent notification spam
- **Missing skip types**: `Call` and `ChannelIconChange` messages now skipped during import
- **`ConnectionError` shadowing**: Renamed to `StoatConnectionError` to avoid shadowing Python builtin
- **GUI resume race condition**: Migration start gated behind `asyncio.Event` until user clicks Resume or Start Fresh
- **Embed/sticker upload errors logged**: Failures now recorded as warnings instead of silently swallowed
- **Version mismatch**: `__init__.py` (0.9.0) and `pyproject.toml` (0.10.0) now aligned to 1.0.0
- **Docs quality pass**: ~30 fixes across all 13 documentation pages — wrong port number, stale stoat-py code examples, missing v0.9.0 flags (--dry-run, --max-channels, --max-emoji), incorrect "Skip threads in GUI" claims, placeholder GitHub URLs, wrong report format, stale resume instructions, inaccurate MigrationEvent/MigrationState descriptions
- **GUI placeholder URL**: Changed `api.revolt.chat` to `api.stoat.chat` in the Stoat API URL input field

### Changed

- **README**: Download links use GitHub Releases `/latest/` pattern, feature table synced with docs, self-hosted tips link added
- **Classifier**: Updated from Beta to Production/Stable

## [0.9.0] — 2026-02-27

### Added

- **Dry-run mode**: `--dry-run` CLI flag and GUI checkbox run all phases without API calls, producing synthetic `dry-*` IDs for validation
- **Configurable server limits**: `--max-channels` and `--max-emoji` CLI options for self-hosted Stoat instances with custom limits
- **Permission bootstrap**: Automatically patches server default role with ferry minimum permissions on server creation
- **GUI resume detection**: Migrate page detects previous `state.json` and offers resume/fresh-start choice
- **GUI attachment size display**: Validate summary now shows total attachment size in human-readable format
- **8 new integration tests** in `test_migrator.py` covering dry-run, permission bootstrap, and configurable limits — 278 total passing

### Fixed

- **ChannelPinnedMessage sent as content**: Pin notification messages are now silenced and the referenced message is queued for re-pinning in the pins phase
- **Failed messages marked as completed**: Removed duplicate `last_completed_message` assignment that caused failed messages to be skipped on resume
- **Missing periodic state saves**: Messages phase now saves state every 50 messages and after each channel completes

### Changed

- **Shared aiohttp session**: Single engine-managed `ClientSession` replaces per-phase sessions for better connection reuse
- **Removed `stoat-py` dependency**: Project already uses custom raw aiohttp API layer; the SDK was unused weight
- **Removed `aiofiles` dependency**: Listed but never imported anywhere in the codebase
- **Engine refactor**: Extracted `_run_phases` from `run_migration` for readability; fixed silent return-value bugs

## [0.8.1] — 2026-02-27

### Fixed

- **skip_threads flag wired to nothing**: `FerryConfig.skip_threads` now filters thread/forum exports in both the channels and messages phases
- **GuildMemberJoin and ThreadCreated not skipped**: Added to `_SKIP_TYPES` so system noise messages are silently dropped per DCE format spec
- **Thread header messages missing**: Flattened thread channels now get a `[Thread migrated from #parent]` system message injected before their content
- **200-channel limit not enforced**: Channels exceeding the Stoat 200-channel limit are now truncated, dropping thread channels first to preserve main channels
- **GUI "Open Report" button broken**: Fixed glob pattern to match the actual `migration_report.json` filename
- **Animated emoji warning missing**: Emits a warning when uploading animated emoji (animation is lost on Stoat)
- **Validate-phase emoji count undercount**: `validate_export` now counts custom emoji from message content in addition to reactions

### Added

- **15 new tests** covering all 7 bug fixes — 270 total passing

### Changed

- **CI pipeline** (`.github/workflows/ci.yml`): lint + type check + test on push/PR with Python 3.10–3.13 matrix via uv, concurrency groups to cancel stale runs
- **Release pipeline** (`.github/workflows/release.yml`): tag-triggered PyInstaller builds for Windows (.exe) and macOS (.app), GitHub Release with attached binaries, PyPI publish via OIDC trusted publisher
- **PyInstaller spec** (`ferry.spec`): NiceGUI asset collection, pywebview native mode support, dynamic version from `__init__.py`, platform-specific icon fallback
- **Getting Started documentation** (`docs/getting-started/`): 4 new pages — install (platform tabs for Windows/macOS/Linux), export-discord (5-step DCE guide with warnings and FAQ), setup-stoat (API URL + token + permissions), first-migration (end-to-end GUI/CLI walkthrough)
- **Docs landing page** (`docs/index.md`): expanded from stub to full landing page with feature table, timing estimates, and guide links
- **Guides documentation** (`docs/guides/`): 5 new pages — gui-walkthrough, cli-reference, large-servers, self-hosted-tips, troubleshooting
- **Reference documentation** (`docs/reference/`): 3 new pages — architecture, stoat-api-notes, dce-format
- **GitHub issue templates** (`.github/ISSUE_TEMPLATE/`): bug report (structured form), feature request, config.yml (template chooser with Discussions link)
- **PR template** (`.github/PULL_REQUEST_TEMPLATE.md`): type-of-change checkboxes and checklist

## [0.8.0] — 2026-02-27

### Added

- **NiceGUI web GUI** (`gui.py`): 3-screen migration workflow — Setup (config form with rate limit slider, skip toggles, advanced options), Validate (export summary table, warnings, ETA estimate, blocks on critical warnings), Migrate (live dashboard with phase chips, progress bar, stats counters, scrolling log, pause/resume, cancel with confirmation dialog, completion card with "Open Report")
- **Pause/cancel support**: `pause_event` and `cancel_event` on `FerryConfig`, engine checks cancel between phases and saves state, message rate limiter respects pause/cancel flags
- **11 GUI tests** covering helper functions (ETA, msgs/hr, summary), cancel-stops-migration, cancel-saves-state, pause-blocks-rate-limiter — 257 total passing

### Fixed

- Hardcoded NiceGUI storage secret replaced with env var / random fallback (`FERRY_STORAGE_SECRET`)

## [0.7.0] — 2026-02-27

### Added

- **CLI interface** (`cli.py`): full Click implementation with `migrate` and `validate` subcommands, Rich progress display with phase status icons, export summary table, ETA estimate, `.env` support via `python-dotenv`, environment variable fallbacks (`STOAT_URL`, `STOAT_TOKEN`), `--resume` / `--skip-*` / `--rate-limit` flags, `MigrationError` and `KeyboardInterrupt` handling with exit codes
- **14 CLI tests** using Click's `CliRunner` with mocked engine — 246 total passing

## [0.6.0] — 2026-02-27

### Added

- **EMOJI phase** (`migrator/emoji.py`): extract unique custom emoji from reactions + content regex, upload to Autumn, create on server with 2s rate-limit delay, resume-safe via `state.emoji_map`
- **MESSAGES phase** (`migrator/messages.py`): full 9-step per-message pipeline — attachment upload (max 5), content transforms (spoilers→underline→mentions→emoji→timestamp→stickers), masquerade with lazy avatar upload/caching, embed flattening, reply references, empty message placeholder, 2000-char truncation, nonce deduplication (`ferry-{msg_id}`), pin/reaction queuing, per-channel resume with numeric Snowflake ID comparison
- **REACTIONS phase** (`migrator/reactions.py`): apply queued reactions with 20-per-message Stoat limit, fire-and-forget error handling
- **PINS phase** (`migrator/pins.py`): restore pinned messages from queue, fire-and-forget error handling
- **4 API functions** (`migrator/api.py`): `api_create_emoji`, `api_send_message`, `api_add_reaction` (URL-encoded emoji), `api_pin_message`
- **73 new tests** across messages (43), emoji (12), API (5+), reactions (6), pins (5) — 233 total passing

## [0.5.0] — 2026-02-26

### Added

- **Stoat API wrapper** (`migrator/api.py`): thin async HTTP layer with retry on 429/5xx, network error handling, and 204 No Content support
- **SERVER phase** (`migrator/structure.py`): creates or attaches to Stoat server, uploads guild icon via Autumn
- **ROLES phase** (`migrator/structure.py`): extracts unique roles from exports, creates with British `colour`, skips @everyone
- **CATEGORIES phase** (`migrator/structure.py`): deduplicates and creates server categories
- **CHANNELS phase** (`migrator/structure.py`): type mapping (text/voice/thread/forum), voice fallback to text, thread name flattening, two-step category assignment, `make_unique_channel_name` collision prevention within 64-char limit
- **33 new tests** across API wrapper (10), structure phases (23) — 160 total passing

## [0.4.0] — 2026-02-26

### Added

- **Autumn uploader** (`uploader/autumn.py`): file upload with size validation per tag, retry on 429/5xx with backoff, and `upload_with_cache` helper backed by `state.upload_cache`
- **CONNECT phase** (`migrator/connect.py`): discovers Autumn URL via `GET /`, verifies auth token via `GET /users/@me`, stores `autumn_url` in migration state
- **Engine default phases** (`core/engine.py`): `_DEFAULT_PHASES` dict for wiring real phase implementations — overrides take priority, then defaults, then skip
- **16 new tests** across uploader (9), connect (6), and engine (1) — 127 total passing

## [0.3.0] — 2026-02-26

### Added

- **Migration engine** (`core/engine.py`): 11-phase orchestrator with phase injection for testing, resume support (skip completed phases), and config-based skip flags for emoji/messages/reactions
- **State persistence** (`state.py`): atomic save/load with JSON round-tripping, crash recovery (state saved on error), author name tracking, counter fields for reactions/pins/attachments
- **Report generator** (`reporter.py`): produces `migration_report.json` per brief §12 with summary counts, ID maps, timing, warnings, and errors
- **Event callback** (`core/events.py`): `EventCallback` type alias and `"skipped"` status for phase events
- **40 new tests** across engine (18), reporter (15), and state (7) — 111 total passing

## [0.2.0] — 2026-02-26

### Added

- **DCE parser** (`parser/dce_parser.py`): parse DiscordChatExporter JSON exports into typed models, with thread/forum inference from filename patterns and export validation (rendered markdown detection, missing media, channel/emoji limits)
- **Content transforms** (`parser/transforms.py`): spoiler conversion, mention/emoji/underline remapping, embed flattening (Discord→Stoat), timestamp formatting, sticker placeholders — all code-block-aware
- **Typed data models** (`parser/models.py`): 10 dataclasses (DCEGuild, DCEChannel, DCEAuthor, DCERole, DCEAttachment, DCEEmoji, DCEReaction, DCEReference, DCEMessage, DCEExport)
- **Test fixtures**: 5 realistic DCE JSON files covering text channels, threads, forums, edge cases, and rendered markdown detection
- **71 passing tests** across parser (27) and transforms (42) with full coverage of edge cases

## [0.1.0] — 2026-02-26

### Added

- Project scaffolding and Claude Code configuration
- Migration engine skeleton (11-phase architecture)
- CLI skeleton (Click)
- GUI skeleton (NiceGUI)
- DCE parser data models (stubs)
- Migration state management dataclass
