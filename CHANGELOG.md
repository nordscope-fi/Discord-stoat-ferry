# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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

- Updated `.claude/rules/dce-format.md` to reflect actual `GuildMemberJoin` (skip) and `ThreadCreated` (skip, header injected instead) behavior
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
- Custom `/ship` skill for commit discipline
