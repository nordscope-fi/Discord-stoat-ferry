# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
