# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

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

## [Unreleased]

### Changed

- Plain English audit across all user-facing docs: define jargon on first use (CLI, JSON, API, CDN, DCE, token, terminal, developer tools), add parenthetical explanations for technical terms, use direct language throughout.
- Comprehensive architecture doc rewrite (`docs/reference/architecture.md`): expanded from ~200 lines to ~1200 lines covering every module, data model, migration phase, API pattern, async design, and design decision.
- Claude Code config cleanup: remove CogniLayer duplication from project CLAUDE.md (~130 lines), remove PostToolUse hook, remove redundant bash/WebFetch permissions, fix tool name typo.
- Overhaul Claude Code workflow pipeline: enforced 8-step chain (`/brief → /spec → /brainstorm → /critique → [/test-scenarios] → writing-plans → build → /ship`) with `<WORKFLOW-GATE>` blocks and Phase 0 prerequisite checks in every skill.
- Clean up public repo: remove internal design docs, briefs, and plans from git tracking; move community files to `.github/`; gitignore local dev config (`.mcp.json`, agent memory).

### Added

- New `/spec` skill: transforms briefs into structured, prioritised requirements with acceptance criteria (between `/brief` and `/brainstorm`).
- New `/brainstorm` skill: project-local design exploration replacing `superpowers:brainstorming`, with correct handoff to `/critique`.
- New `/test-scenarios` skill: generates test scenarios from spec acceptance criteria (optional, recommended for Large tasks).
- Key-files and security rules (`.claude/rules/key-files.md`, `.claude/rules/security.md`).
- CogniLayer MCP wiring (`.mcp.json`, tool permissions) for two-layer memory model.
- Change manifest pattern for `/ship` audit step (`.claude/change-manifest.md` template in CLAUDE.md).

### Fixed

- Fix misleading "bot token" terminology across entire project: GUI labels, CLI help, all user-facing docs, code comments, and reference docs now consistently say "user token" with plain-English explanations for non-technical users. Expanded token setup guide with step-by-step browser instructions and Local Storage troubleshooting.
- Exclude internal design docs (`docs/plans/`, brief) from public docs site via `exclude_docs`.
- Add dark/light mode toggle and GitHub repo link to docs site theme.

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
- **Documentation**: Updated `stoat-api-notes.md` and `.claude/rules/stoat-api.md` with correct endpoints, string limits, and deprecation notes.

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

- **`/brief` skill**: 6-phase requirements crystallization for the design pipeline (`.claude/skills/brief/`)
- **`/critique` skill**: 7-dimension design review adapted for Discord Ferry constraints (`.claude/skills/critique/`)
- **PostToolUse hook**: Reminds about verification batching during multi-file edits
- **SessionStart version display**: Shows current project version on session start
- **Context7 library ID table**: Known IDs for faster documentation lookups
- **Ship skill Step 4**: Uses `get_mistral_opinion` with file-category focus instead of `get_default_opinion`
- **Ship skill Step 3**: Added "historically skipped" warning and Skill-vs-Task clarification
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
