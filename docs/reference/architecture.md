# Architecture

This is the comprehensive technical reference for Discord Ferry's internals. It covers every
module, data model, async pattern, and design decision in the codebase. If you want to
contribute code, understand a bug, or build a new integration, start here.

For a quick introduction to what Ferry does and how to use it, see the
[Getting Started](../getting-started/install.md) guides instead.

---

## Overview

Discord Ferry is a Python 3.10+ migration tool that moves a Discord server to
[Stoat](https://stoat.chat) (formerly Revolt). It reads DiscordChatExporter (DCE) JSON
exports, transforms them into Stoat API calls, and sends everything — messages, channels,
roles, emoji, attachments, permissions — to the target Stoat instance.

**Tech stack**: aiohttp (HTTP client), NiceGUI (web GUI), Click (CLI), Rich (formatted
terminal output), ijson (streaming JSON), pytest + pytest-asyncio (tests), ruff (lint/format),
mypy strict (types), PyInstaller (binary packaging).

**Core design principle**: one engine, two shells. The migration engine (`core/engine.py`)
contains all logic. It never imports from the GUI or CLI. The GUI and CLI are thin wrappers
that configure the engine, subscribe to its event stream, and render progress in their own way.

---

## Project Layout

```
src/discord_ferry/
├── __init__.py              # Package version (__version__)
├── config.py                # FerryConfig dataclass — all runtime settings
├── state.py                 # MigrationState dataclass — ID maps, counters, resume checkpoints
├── errors.py                # Custom exception hierarchy (FerryError base)
├── reporter.py              # Post-migration report generator
├── review.py                # Pre-creation review summary builder
├── blueprint.py             # Server blueprint export/import
├── gui.py                   # NiceGUI web interface (4-screen workflow)
├── cli.py                   # Click CLI (migrate, validate, build, export-blueprint)
│
├── core/
│   ├── engine.py            # Migration orchestrator — runs all 12 phases
│   ├── events.py            # MigrationEvent dataclass and EventCallback type
│   └── security.py          # SecureTokenStore — token masking and sanitization at output boundaries
│
├── parser/
│   ├── models.py            # 10 DCE dataclasses (DCEExport, DCEMessage, etc.)
│   ├── dce_parser.py        # JSON parsing, validation, streaming
│   └── transforms.py        # Content transforms (mentions, emoji, embeds, polls, stickers)
│
├── migrator/
│   ├── api.py               # Stoat REST API wrapper with retry + rate limit handling
│   ├── connect.py           # Phase 2: CONNECT — test credentials, discover Autumn URL
│   ├── structure.py         # Phases 3–6: SERVER, ROLES, CATEGORIES, CHANNELS
│   ├── emoji.py             # Phase 7: EMOJI — extract, upload, register
│   ├── avatars.py           # Phase 7.5: AVATARS — pre-flight avatar download + Autumn upload
│   ├── messages.py          # Phase 8: MESSAGES — 9-step per-message pipeline
│   ├── reactions.py         # Phase 9: REACTIONS — apply queued reactions
│   └── pins.py              # Phase 10: PINS — re-pin messages
│
├── uploader/
│   └── autumn.py            # Autumn (Stoat file storage) upload client with caching
│
├── discord/
│   ├── __init__.py          # fetch_and_translate_guild_metadata() orchestration
│   ├── client.py            # Async HTTP client for Discord REST API v10
│   ├── models.py            # DiscordRole, DiscordChannel, PermissionOverwrite
│   ├── permissions.py       # Discord → Stoat permission bit translation
│   └── metadata.py          # DiscordMetadata persistence (discord_metadata.json)
│
├── exporter/
│   ├── __init__.py          # Public API
│   ├── manager.py           # DCE binary download and version management
│   └── runner.py            # Async subprocess execution and progress parsing
│
└── templates/
    ├── gaming.json          # Preset: gaming server template
    ├── community.json       # Preset: community server template
    └── education.json       # Preset: education server template
```

### Module Dependency Graph

```
gui.py ──────┐
             ├──→ core/engine.py ──→ core/events.py
cli.py ──────┘         │
                       ├──→ config.py, state.py, errors.py
                       │
                       ├──→ exporter/ (DCE binary + subprocess)
                       ├──→ parser/ (JSON parsing + transforms)
                       ├──→ discord/ (Discord API + permission translation)
                       ├──→ review.py (pre-creation summary)
                       │
                       ├──→ migrator/connect.py
                       ├──→ migrator/structure.py ──→ migrator/api.py
                       ├──→ migrator/emoji.py ─────→ migrator/api.py, uploader/
                       ├──→ migrator/avatars.py ───→ uploader/
                       ├──→ migrator/messages.py ──→ migrator/api.py, uploader/, parser/transforms
                       ├──→ migrator/reactions.py ─→ migrator/api.py
                       ├──→ migrator/pins.py ──────→ migrator/api.py
                       │
                       └──→ reporter.py (post-migration report)
```

**Key constraint**: `engine.py` never imports from `gui.py` or `cli.py`. The GUI and CLI
only import from `core/`, `config.py`, and `state.py`.

---

## Data Models

### FerryConfig (`config.py`)

All runtime configuration in a single dataclass. Created by the GUI or CLI, passed to the
engine. Never persisted to disk.

```python
@dataclass
class FerryConfig:
    # Required credentials
    export_dir: Path                 # DCE JSON exports directory
    stoat_url: str                   # Stoat API base URL
    token: str                       # Stoat user token (repr=False — hidden in logs)

    # Optional input
    server_id: str | None            # Attach to existing Stoat server (else create new)
    server_name: str | None          # Override server display name
    dry_run: bool                    # Preview without API calls
    skip_messages: bool              # Structure-only migration
    skip_emoji: bool                 # Skip emoji upload
    skip_reactions: bool             # Skip reaction application
    skip_threads: bool               # Skip thread/forum flattening
    message_rate_limit: float        # Seconds per message (default 1.0)
    upload_delay: float              # Seconds between Autumn uploads (default 0.5)
    output_dir: Path                 # Where state.json and report go
    resume: bool                     # Load existing state and continue
    verbose: bool                    # Extra logging for CLI
    max_channels: int                # Stoat channel limit (default 200)
    max_emoji: int                   # Stoat emoji limit (default 100)
    checkpoint_interval: int         # State save frequency during messages (default 50, min 1, throttled max 1/5s)
    skip_avatars: bool               # Skip the avatar pre-flight phase (default False)
    reaction_mode: str               # "text" (default), "native" (Phase 9 API), or "skip"
    min_thread_messages: int         # Filter threads below this count (0 = include all)
    validate_after: bool             # Run post-migration validation phase (default False)
    max_concurrent_requests: int     # asyncio.Semaphore bound for API requests (default 5, min 1)

    # Discord API (orchestrated mode only)
    discord_token: str | None        # Discord user token (repr=False)
    discord_server_id: str | None    # Source Discord guild ID

    # Runtime control (set by engine, not by user)
    skip_export: bool                # Don't run DCE subprocess
    pause_event: asyncio.Event | None   # GUI: block until user resumes
    cancel_event: asyncio.Event | None  # User stopped migration
    session: aiohttp.ClientSession | None  # Shared HTTP session (set by engine)
```

### MigrationState (`state.py`)

Tracks everything the engine produces across phases. Serialised to `state.json` after each
phase for crash recovery and resume.

```python
@dataclass
class MigrationState:
    # ID mappings (Discord ID → Stoat ID)
    role_map: dict[str, str]
    channel_map: dict[str, str]
    category_map: dict[str, str]
    message_map: dict[str, str]         # For reply reference resolution — saved to message_map.json
    emoji_map: dict[str, str]

    # Upload caches (avoid re-uploading identical files)
    avatar_cache: dict[str, str]        # User ID → Autumn avatar file ID
    upload_cache: dict[str, str]        # Local file path → Autumn file ID

    # Author context (for mention remapping in transforms)
    author_names: dict[str, str]        # Discord user ID → display name

    # Deferred operations (collected during MESSAGES, applied in later phases)
    pending_pins: list[tuple[str, str]]         # (stoat_channel_id, stoat_message_id)
    pending_reactions: list[dict[str, str]]      # {channel_id, message_id, emoji}

    # Dead-letter queue (messages that failed after retries)
    failed_messages: list[FailedMessage]  # discord_msg_id, stoat_channel_id, error, retry_count, content_preview

    # Autumn upload tracking (for orphan detection and resume)
    autumn_uploads: dict[str, str]        # Autumn file_id → source_id
    referenced_autumn_ids: set[str]       # Autumn IDs confirmed used in sent messages (serialised as list)

    # Post-migration validation results
    validation_results: dict[str, object] # Channel/role count comparisons from VALIDATE MIGRATION phase

    # Logs (structured dicts with phase, type, and message)
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]

    # Server discovery
    stoat_server_id: str
    autumn_url: str                     # Discovered during CONNECT phase

    # Forum/thread tracking
    forum_index_message_ids: dict[str, str]   # forum channel ID → index message Stoat ID
    forum_channel_members: dict[str, list[str]]  # forum channel ID → list of thread channel IDs
    forum_category_names: dict[str, str]      # forum channel ID → category name on Stoat

    # Resume tracking
    current_phase: str                  # Phase name for resume skip logic
    completed_channel_ids: set[str]     # Discord channel IDs fully processed in MESSAGES phase
    channel_message_offsets: dict[str, str]  # Partial channel → last processed Discord message ID

    # Counters
    attachments_uploaded: int
    attachments_skipped: int
    reactions_applied: int
    pins_applied: int
    channel_message_counts: dict[str, int]   # Stoat channel ID → messages sent count
    prior_messages_total: int                # Total messages in prior (resumed) state
    embeds_total: int
    embeds_dropped: int
    replies_linked: int
    replies_total: int

    # Timing
    started_at: str                     # ISO 8601
    completed_at: str                   # ISO 8601

    # Flags
    is_dry_run: bool                    # Reject resume from dry-run state
    export_completed: bool              # Smart export phase skipping
```

**Persistence**: `save_state()` writes atomically (temp file + rename). `load_state()` reads
and validates. Both use `_state_to_dict()` / `_dict_to_state()` for JSON round-tripping.

### DCE Parser Models (`parser/models.py`)

Ten dataclasses representing DiscordChatExporter JSON structure:

| Dataclass | Key Fields |
|-----------|-----------|
| `DCEGuild` | `id`, `name`, `icon_url` |
| `DCEChannel` | `id`, `type`, `name`, `category_id`, `category`, `topic` |
| `DCEAuthor` | `id`, `name`, `discriminator`, `nickname`, `color`, `is_bot`, `avatar_url`, `roles` |
| `DCERole` | `id`, `name`, `color`, `position` |
| `DCEAttachment` | `id`, `url`, `file_name`, `file_size_bytes` |
| `DCEEmoji` | `id`, `name`, `is_animated`, `image_url` |
| `DCEReaction` | `emoji` (DCEEmoji), `count` |
| `DCEReference` | `message_id`, `channel_id`, `guild_id` |
| `DCEMessage` | `id`, `type`, `timestamp`, `content`, `author`, `is_pinned`, `attachments`, `embeds`, `stickers`, `reactions`, `mentions`, `reference`, `poll` |
| `DCEExport` | `guild`, `channel`, `messages`, `message_count`, `exported_at`, `is_thread`, `parent_channel_name`, `json_path` |

### Discord Metadata Models (`discord/models.py`, `discord/metadata.py`)

```python
PermissionOverwrite(id, type, allow, deny)     # type: 0=role, 1=member
DiscordRole(id, name, permissions, position, color, hoist, managed)
DiscordChannel(id, name, type, nsfw, permission_overwrites)

PermissionPair(allow, deny)                    # In Stoat bit space (translated)
RoleOverride(discord_role_id, allow, deny)     # Per-role channel override
ChannelMeta(nsfw, default_override, role_overrides)

DiscordMetadata(                               # Persisted to discord_metadata.json
    guild_id, fetched_at,
    server_default_permissions,                # @everyone server-wide permissions
    role_permissions,                          # {role_id: PermissionPair}
    channel_metadata,                          # {channel_id: ChannelMeta}
)
```

### Blueprint Models (`blueprint.py`)

```python
BlueprintRole(name, colour, permissions, rank)
BlueprintChannel(name, type, nsfw)             # type: "Text" or "Voice"
BlueprintCategory(name, channels)
ServerBlueprint(name, description, roles, categories, uncategorized_channels)
```

Blueprints use **names, not IDs** — making them portable across Stoat instances.

---

## Migration Pipeline

### Phase Execution

The engine runs 12 phases in strict order. Each phase depends on ID mappings and state
produced by earlier phases.

```
EXPORT → VALIDATE → CONNECT → SERVER → ROLES → CATEGORIES → CHANNELS
                                                                 ↓
  VALIDATE MIGRATION ← REPORT ← PINS ← REACTIONS ← MESSAGES ← EMOJI → AVATARS
```

| # | Phase | Module | Reads | Writes | API Calls |
|---|-------|--------|-------|--------|-----------|
| 0 | **EXPORT** | `exporter/` | Discord API | DCE JSON files | Discord API |
| 1 | **VALIDATE** | `parser/` | DCE JSON files | warnings, author_names | None |
| 2 | **CONNECT** | `migrator/connect.py` | config | autumn_url, stoat_server_id | `GET /` + `GET /users/@me` |
| 3 | **SERVER** | `migrator/structure.py` | exports[0].guild | stoat_server_id | `POST /servers`, Autumn upload |
| 4 | **ROLES** | `migrator/structure.py` | exports, discord_metadata | role_map | `POST /servers/:id/roles`, permission PUTs |
| 5 | **CATEGORIES** | `migrator/structure.py` | exports | category_map | `PATCH /servers/:id` |
| 6 | **CHANNELS** | `migrator/structure.py` | exports, discord_metadata | channel_map | `POST /servers/:id/channels`, permission PUTs |
| 7 | **EMOJI** | `migrator/emoji.py` | message content + reactions | emoji_map | Autumn upload + `PUT /servers/:id/emoji/:id` |
| 7.5 | **AVATARS** | `migrator/avatars.py` | all exports (unique authors) | avatar_cache | Autumn upload |
| 8 | **MESSAGES** | `migrator/messages.py` | all messages | message_map, pending_pins/reactions | `POST /channels/:id/messages`, Autumn uploads |
| 9 | **REACTIONS** | `migrator/reactions.py` | pending_reactions | reactions_applied | `PUT /channels/:id/messages/:id/reactions` |
| 10 | **PINS** | `migrator/pins.py` | pending_pins | pins_applied | `PUT /channels/:id/messages/:id/pin` |
| 11 | **REPORT** | `reporter.py` | state | migration_report.json | None |
| 12 | **VALIDATE MIGRATION** | inline in `engine.py` | state, config | validation_results | `GET /servers/:id` |

### Phase Skip Logic

Phases can be skipped in three ways:

1. **Config flags**: `skip_messages`, `skip_emoji`, `skip_reactions`, `skip_threads`, `skip_export`, `skip_avatars`; `reaction_mode="skip"` for REACTIONS; `validate_after=False` (default) for VALIDATE MIGRATION
2. **Resume**: If `state.current_phase` index > phase index, the phase was already completed
3. **Mode**: EXPORT phase is skipped entirely in offline mode (no Discord token)

### Review Gate

Between VALIDATE and SERVER, the engine inserts a review step:

1. `review.py` builds a `ReviewSummary` from parsed exports and Discord metadata
2. Engine emits a `MigrationEvent` with `status="confirm"` and the summary as `detail`
3. **GUI**: Blocks on `pause_event` until user clicks Proceed or Cancel
4. **CLI**: Prints a Rich table and continues (non-interactive)

### Per-Phase Detail

**EXPORT** (Phase 0): Runs inline in `run_migration()`. Downloads the DCE binary if not
cached, validates the Discord token, then runs DCE as a subprocess. Progress is parsed from
DCE's stdout and emitted as events. Produces DCE JSON files in `config.export_dir`.

**VALIDATE** (Phase 1): Runs inline. Calls `parse_export_directory(metadata_only=True)` which
reads channel metadata but skips loading messages into memory. Then calls `validate_export()`
which streams messages to check for issues (rendered markdown, missing media, duplicate IDs).
Collects `author_names` in a single pass to avoid re-scanning later.

**CONNECT** (Phase 2): `GET /` on the Stoat API to discover the Autumn upload URL from the
`features.autumn.url` field. `GET /users/@me` to validate the token. If `config.server_id` is
set, verifies the server is accessible (best-effort, non-fatal on failure).

**SERVER** (Phase 3): Creates a new server via `POST /servers` (or verifies the existing one).
Uploads the guild icon to Autumn and applies it. Sets server default permissions to
`FERRY_MIN_PERMISSIONS` (1,022,361,624) to ensure the Ferry account can operate.

**ROLES** (Phase 4): Iterates all exports to collect unique role IDs (skipping @everyone where
`role_id == guild_id`). Creates each role with name and British-spelled `colour`. If Discord
metadata is available, applies translated permission bits via `api_set_role_permissions()`.
Attempts rank ordering in a second pass using DCE position data. Populates `state.role_map`.

**CATEGORIES** (Phase 5): Collects unique category names from exports. Creates each category
via the two-step process: create a channel-like object, then PATCH the server's `categories`
array. Populates `state.category_map`.

**CHANNELS** (Phase 6): Per export, creates a TextChannel or VoiceChannel. Sets NSFW flag from
Discord metadata. Assigns to category via the two-step PATCH. Flattens threads into standalone
text channels. Groups forum/media threads into dedicated categories named after the parent
forum. Applies channel-level permission overrides (@everyone as default, per-role as overrides).
Populates `state.channel_map`.

**EMOJI** (Phase 7): Scans all messages (via streaming if `metadata_only`) for custom emoji in
content (`<:name:ID>`) and reactions. Deduplicates by ID. Downloads each emoji image, uploads
to Autumn with tag `emojis`, creates on server. 2.0s delay between creates (shares the 5/10s
`/servers` rate bucket). Populates `state.emoji_map`.

**AVATARS** (Phase 7.5): Pre-flight avatar download and upload. `run_avatars()` in
`migrator/avatars.py` scans all exports for unique authors, downloads each author's avatar
(local file or remote URL), uploads to Autumn with the `avatars` tag, and populates
`state.avatar_cache` (user ID → Autumn file ID). This front-loads all avatar uploads before
the MESSAGES phase begins, so message sends never block on avatar I/O. Skippable via
`skip_avatars` config flag. Added in v1.5.0.

**MESSAGES** (Phase 8): The largest phase. In v2.0.0 this phase uses a **parallel channel
architecture**:

- Channels are processed concurrently via `asyncio.gather` bounded by
  `asyncio.Semaphore(config.max_concurrent_channels)` (default 3)
- Each channel worker accumulates results in a **`ChannelResult` dataclass** containing:
  warnings, errors, failed_messages, message_map_updates, pending_pins, pending_reactions,
  and attachment counters
- Results from each worker are merged into the main `MigrationState` after the channel
  completes (serialized under a `save_lock: asyncio.Lock`)
- **Thread merge mode** (`thread_strategy="merge"`) processes thread channels sequentially
  AFTER all parent channels finish, so parent message IDs are available for reply linking

Per channel, messages are streamed oldest-first. Per message (9-step pipeline):

1. Check skip types → skip system messages (join, boost, etc.)
2. If `ChannelPinnedMessage` → extract reference, add to `pending_pins`
3. Transform content (spoilers → underline → mentions → emoji → timestamps)
4. Upload attachments to Autumn (max 5 per message)
5. Flatten embeds (extract media, convert to Stoat format)
6. Handle stickers (upload image or text fallback)
7. Render polls as formatted text
8. Build masquerade (author name + avatar + colour)
9. Send via `api_send_message` with `Idempotency-Key` header `ferry-{discord_msg_id}`

Collects `pending_reactions` for Phase 9. Saves state every `checkpoint_interval` messages
(default 50, time-throttled to at most once per 5 seconds via `save_lock`). Completed channel
IDs are recorded in `state.completed_channel_ids`.

**REACTIONS** (Phase 9): Iterates `state.pending_reactions`. Calls `api_add_reaction` for each.
Enforces the 20-reactions-per-message Stoat limit. Fire-and-forget error handling (failures
logged as warnings, do not stop migration).

**PINS** (Phase 10): Iterates `state.pending_pins` tuples. Calls `api_pin_message` for each.
Fire-and-forget error handling.

**REPORT** (Phase 11): Runs inline. Calls `generate_report()` which writes
`migration_report.json` containing summary counts, ID maps, timing, warnings, errors, and a
dynamic post-migration checklist.

**VALIDATE MIGRATION** (Phase 12, optional): Runs inline in `engine.py` only when
`validate_after=True`. Queries the Stoat server via `api_fetch_server()` and compares
channel/role counts against state maps. Reports discrepancies (missing channels, extra roles,
etc.) in `state.validation_results`. Does not modify any data on the server. Added in v1.6.0.

---

## Event System

### MigrationEvent

```python
@dataclass
class MigrationEvent:
    phase: str              # "export", "validate", "messages", etc.
    status: str             # See table below
    message: str            # Human-readable status text
    current: int = 0        # Progress: items processed so far
    total: int = 0          # Progress: total items in this phase
    channel_name: str = ""  # Currently active channel (if applicable)
    detail: dict | None     # Extra context (review summary, error info)
```

### Status Values

| Status | Meaning | Emitted by |
|--------|---------|-----------|
| `started` | Phase has begun | Engine, at phase entry |
| `progress` | Work in progress | Phase implementations, repeatedly |
| `completed` | Phase finished successfully | Engine, at phase exit |
| `error` | Fatal error occurred | Phase implementations |
| `warning` | Non-fatal issue | Phase implementations |
| `skipped` | Phase was skipped (config flag or resume) | Engine |
| `confirm` | Awaiting user confirmation (review gate) | Engine, once |

### How Shells Subscribe

```python
# Engine signature
async def run_migration(
    config: FerryConfig,
    on_event: EventCallback,          # GUI or CLI provides this
    phase_overrides: dict | None,     # For testing: inject mock phases
) -> MigrationState
```

**GUI** passes a callback that updates NiceGUI UI elements (progress bars, status chips, log
entries). Uses `ui.timer` to drive async updates in NiceGUI's event loop.

**CLI** passes a `_ProgressTracker` instance that renders Rich progress bars, status tables,
and prints warnings/errors to the console via `Rich.Live`.

Neither shell knows about the other. Adding a new UI (REST API, TUI, etc.) only requires
writing a new callback — no engine changes needed.

---

## Stoat API Layer (`migrator/api.py`)

### Function Inventory

| Function | HTTP | Purpose |
|----------|------|---------|
| `api_create_server` | `POST /servers` | Create new Stoat server |
| `api_fetch_server` | `GET /servers/:id` | Verify server exists |
| `api_edit_server` | `PATCH /servers/:id` | Update server settings |
| `api_create_role` | `POST /servers/:id/roles` | Create role |
| `api_edit_role` | `PATCH /servers/:id/roles/:id` | Update role (colour, rank) |
| `api_set_role_permissions` | `PUT /servers/:id/permissions/:id` | Set role permission bits |
| `api_set_server_default_permissions` | `PUT /servers/:id/permissions/default` | Set @everyone server permissions |
| `api_create_channel` | `POST /servers/:id/channels` | Create channel |
| `api_upsert_categories` | `PATCH /servers/:id` | Set full categories array on server |
| `api_set_channel_default_permissions` | `PUT /channels/:id/permissions/default` | Set @everyone channel override |
| `api_set_channel_role_permissions` | `PUT /channels/:id/permissions/:role_id` | Set per-role channel override |
| `api_send_message` | `POST /channels/:id/messages` | Send message with masquerade |
| `api_edit_message` | `PATCH /channels/:id/messages/:id` | Edit a sent message (content, embeds) |
| `api_add_reaction` | `PUT /channels/:id/messages/:id/reactions/:emoji` | Add reaction |
| `api_create_emoji` | `PUT /custom/emoji/:id` | Register emoji on server |
| `api_pin_message` | `PUT /channels/:id/messages/:id/pin` | Pin a message |

### String Sanitization (`migrator/sanitize.py`)

The Stoat API enforces a **32-character maximum** on all name fields. Ferry sanitizes at call
sites (not inside API wrappers) using two helpers:

| Helper | Applied to | Rules |
|--------|-----------|-------|
| `truncate_name(name, max_length=32)` | Channel names, role names, category titles, masquerade display names | Truncate to 32 chars |
| `sanitize_emoji_name(name)` | Custom emoji names | Lowercase, replace non-`[a-z0-9_]` with `_`, strip edges, truncate to 32, fallback to `"emoji"` if empty |

Channel name collisions after truncation are handled by `make_unique_channel_name()` which
appends `-1`, `-2` suffixes (eating into the 32-char budget as needed).

### Rate Limit Buckets

Stoat uses **fixed 10-second windows** (not sliding).

| Bucket | Limit | Shared across |
|--------|-------|---------------|
| `/servers` | **5 per 10s** | Server create, channel create, role create, emoji create, category edit |
| Messages | **10 per 10s** | `POST /channels/:id/messages` only |
| Catch-all | **20 per 10s** | Everything else, including Autumn uploads |

!!! warning "The /servers bucket is shared"
    Creating a channel, a role, and an emoji in quick succession all draw from the same
    5-per-10-second budget. Ferry paces structure creation phases to stay within this limit.

**Response headers**: Stoat returns **no rate limit headers**. There are no
`X-RateLimit-Remaining`, `X-RateLimit-Reset-After`, or `X-RateLimit-Bucket` headers on any
response.

**429 response body**: `{ "retry_after": 4200 }` — Ferry sleeps for this duration (milliseconds)
and retries. In addition, Ferry's adaptive 429-frequency optimizer tracks 429 frequency in a
rolling 60-second window and auto-adjusts a delay multiplier (1.5× increase on burst, 0.75×
decay when clear).

### Retry Logic

`_api_request()` retries up to 3 times on:

- **429 Too Many Requests**: Sleep for `retry_after` milliseconds from response body
- **502 Bad Gateway**, **503 Service Unavailable**, **504 Gateway Timeout**: Exponential backoff

All other HTTP errors raise `MigrationError` immediately.

### British Spelling

The Stoat API uses British English. Using American spelling causes silent failures.

| Always use | Never use |
|-----------|----------|
| `colour` | `color` |
| `ManageCustomisation` | `ManageCustomization` |

This applies to masquerade payloads, embed objects, role objects, and permission names.

### Permission Bits

Stoat has **no single ADMINISTRATOR permission**. Every capability must be granted individually.

| Name | Bit | Value | Notes |
|------|-----|-------|-------|
| ManageChannel | 0 | 1 | |
| ManageServer | 1 | 2 | |
| ManagePermissions | 2 | 4 | |
| ManageRole | 3 | 8 | Also required for masquerade `colour` |
| ManageCustomisation | 4 | 16 | Required to create/manage emoji |
| ViewChannel | 20 | 1,048,576 | |
| ReadMessageHistory | 21 | 2,097,152 | |
| SendMessage | 22 | 4,194,304 | |
| ManageMessages | 23 | 8,388,608 | Required to pin messages |
| SendEmbeds | 26 | 67,108,864 | |
| UploadFiles | 27 | 134,217,728 | |
| Masquerade | 28 | 268,435,456 | Required for masquerade name and avatar |
| React | 29 | 536,870,912 | |

**Ferry minimum permissions** (bits 3, 4, 20–23, 26–29):

```python
FERRY_MIN_PERMISSIONS = (
    8 | 16 | 1_048_576 | 2_097_152 | 4_194_304
    | 8_388_608 | 67_108_864 | 134_217_728 | 268_435_456 | 536_870_912
)  # == 1_022_361_624
```

### Category Management Pattern

Categories in Stoat live on the **Server object**, not on channels. There is no `category_id`
parameter on channel creation. The process is:

1. Create all channels via `POST /servers/:id/channels`
2. Build the full categories array locally (each category has a client-generated ID, title, and channel list)
3. PATCH the server with `{"categories": [...]}` in a single call via `api_upsert_categories`

```python
channel = await api_create_channel(session, stoat_url, token, server_id, name="general")
categories = [
    {"id": uuid4().hex[:26], "title": "Text Channels", "channels": [channel["_id"]]},
]
await api_upsert_categories(session, stoat_url, token, server_id, categories)
```

Category IDs are generated client-side (`uuid4().hex[:26]`). The PATCH replaces the entire
categories array on the server.

---

## Parser and Transforms

### DCE JSON Parsing (`parser/dce_parser.py`)

**Full parse** (`parse_export_directory(export_dir)`): Reads all `*.json` files in the
directory, parses guild, channel, and message data into typed dataclasses.

**Metadata-only parse** (`parse_export_directory(export_dir, metadata_only=True)`): Reads
guild and channel data but **skips the messages array**. Sets `json_path` and `message_count`
on each `DCEExport` so messages can be streamed later. This is the default mode — it keeps
memory flat for large exports.

**Streaming** (`stream_messages(json_path)`): Uses `ijson.items()` to iterate messages one at
a time with O(1) memory per message. Used by VALIDATE (for scanning), EMOJI (for extraction),
and MESSAGES (for import) when running in metadata-only mode.

**Validation** (`validate_export(exports, export_dir, author_names)`): Single-pass scan that
checks for rendered markdown detection (missing `--markdown false`), missing media files,
duplicate channel IDs, and limit violations. Populates `author_names` dict to avoid a second
scan.

### Thread Inference

DCE does not include thread-to-parent relationships in JSON metadata. Ferry infers them from
filenames:

- `{Guild} - {Channel} [{id}].json` → regular channel (2 dash-separated segments)
- `{Guild} - {Parent} - {Thread} [{id}].json` → thread or forum post (3 segments)

The `DCEExport` dataclass stores `is_thread` and `parent_channel_name` based on this inference.

### Content Transforms (`parser/transforms.py`)

All transforms are **code-block-aware**: they split content on ` ```...``` ` and `` `...` ``
blocks, apply the transformation only to non-code regions, then reassemble. This prevents
mangling code snippets in messages.

**Transform pipeline** (applied in order during MESSAGES phase):

| Transform | Input | Output | Notes |
|-----------|-------|--------|-------|
| `convert_spoilers` | `\|\|text\|\|` | `!!text!!` | Discord → Stoat spoiler syntax |
| `strip_underline` | `__text__` | `text` | Stoat does not support underline |
| `remap_mentions` | `<@ID>`, `<#ID>`, `<@&ID>` | Display name or Stoat ID | Uses channel_map, role_map, author_names |
| `remap_emoji` | `<:name:ID>` | `:stoat_id:` or `[:name:]` | Uses emoji_map, fallback for unmapped |
| `format_original_timestamp` | ISO 8601 | `*[2024-01-15 14:30 UTC]*` | Prepended to message body |
| `flatten_embed` | Discord embed dict | Stoat SendableEmbed + media path | Author, fields, footer → description |
| `flatten_poll` | Poll dict | Formatted text | Question + options with vote counts |
| `handle_stickers` | Sticker list | Text reference or upload path | Image if local, `[Sticker: name]` fallback |

### Embed Flattening

Discord embeds have a richer structure than Stoat embeds. `flatten_embed()` converts:

- `embed.author.name` → first line of description
- `embed.fields` → appended to description as `**Name**: Value`
- `embed.footer.text` → last line of description
- `embed.thumbnail.url` or `embed.image.url` → returns local media path for Autumn upload

The result is a Stoat-compatible embed dict plus an optional local file path for the media.

---

## File Upload — Autumn (`uploader/autumn.py`)

Autumn is Stoat's media storage service. It accepts file uploads via multipart form POST and
returns a file ID that can be referenced in messages, avatars, and emoji.

**Critical constraint**: Autumn cannot fetch URLs. You must download the file locally first,
then upload it as multipart form data.

### Upload Flow

```
Local file → validate size → POST multipart to {autumn_url}/{tag} → file ID
```

### Size Limits by Tag

| Tag | Max Size | Used for |
|-----|---------|---------|
| `attachments` | 20 MB | Message attachments |
| `avatars` | 4 MB | User/masquerade avatars |
| `icons` | 2.5 MB | Server icon, role icon |
| `banners` | 6 MB | Server banner |
| `emojis` | 500 KB | Custom emoji |

### Upload Cache

`upload_with_cache()` maintains an in-memory cache keyed on local file path. If the same file
appears multiple times (e.g. an author who appears in thousands of messages), the avatar is
uploaded once and the Autumn file ID is reused for all subsequent messages.

A conservative 0.5s sleep is inserted between uploads to avoid bursting the catch-all rate
limit bucket.

---

## Discord Integration (`discord/`)

The `discord/` package is only active when both `discord_token` and `discord_server_id` are
provided (1-Click mode). In offline mode, it is skipped entirely and permissions are not
migrated.

### What It Fetches

1. **Guild roles** (`GET /guilds/{id}/roles`): All roles with permission bitfields, position,
   colour, managed flag
2. **Guild channels** (`GET /guilds/{id}/channels`): All channels with NSFW flag and
   per-channel permission overwrites

!!! note "Permissions come as strings"
    Discord's REST API returns permission bitfields as **strings** (e.g. `"2048"`), not
    integers. The client calls `int()` when parsing.

### Permission Translation (`discord/permissions.py`)

`translate_permissions(discord_bits: int) -> int` converts a Discord permission bitfield to
the equivalent Stoat bitfield.

| Discord Permission | Discord Bit | Stoat Permission | Stoat Bit |
|-------------------|-------------|-----------------|-----------|
| MANAGE_CHANNELS | 4 | ManageChannel | 0 |
| MANAGE_GUILD | 5 | ManageServer | 1 |
| MANAGE_ROLES | 28 | ManagePermissions + ManageRole | 2, 3 |
| MANAGE_EMOJIS | 30 | ManageCustomisation | 4 |
| VIEW_CHANNEL | 10 | ViewChannel | 20 |
| READ_MESSAGE_HISTORY | 16 | ReadMessageHistory | 21 |
| SEND_MESSAGES | 11 | SendMessage | 22 |
| MANAGE_MESSAGES | 13 | ManageMessages | 23 |
| EMBED_LINKS | 14 | SendEmbeds | 26 |
| ATTACH_FILES | 15 | UploadFiles | 27 |
| ADD_REACTIONS | 6 | React | 29 |

**Special cases**:

- **ADMINISTRATOR** (Discord bit 3): Expands to `ALL_STOAT_PERMISSIONS` — every Stoat bit set
- **MANAGE_ROLES** maps to **two** Stoat bits (ManagePermissions + ManageRole)
- **Unmapped Discord bits**: Silently dropped (no Stoat equivalent)
- **Managed/bot roles**: Permissions skipped (role still created for mention remapping)

### @everyone Handling

Discord's @everyone role has `id == guild_id`. In channel permission overwrites, this must be
extracted as the channel's `default_override` and applied via Stoat's separate
`PUT /channels/:id/permissions/default` endpoint. If it goes into `role_overrides`, it silently
drops because `guild_id` is never in `role_map`.

### Metadata Persistence

`DiscordMetadata` is saved to `discord_metadata.json` alongside `state.json`. This ensures
permission data survives resume — the Discord API does not need to be re-queried.

---

## Message Pipeline (`migrator/messages.py`)

The MESSAGES phase is the most complex. It processes messages oldest-first, per channel, with
full content transformation and author attribution.

### Per-Message Processing (9 Steps)

```
1. Type check         → skip system messages (join, boost, pin notification, etc.)
2. Pin detection      → if ChannelPinnedMessage, extract reference for pending_pins
3. Content transforms → spoilers, underline, mentions, emoji, timestamps
4. Attachment upload   → download local file, upload to Autumn (max 5 per message)
5. Embed flattening   → convert Discord embed to Stoat format, upload media
6. Sticker handling   → upload image or generate text fallback
7. Poll rendering     → convert poll data to formatted text in message body
8. Masquerade build   → author name + avatar (lazy upload) + colour
9. Send               → api_send_message with Idempotency-Key for deduplication
```

### Author Attribution (Masquerade)

Every message is sent with a masquerade payload that makes it appear to come from the original
Discord author:

```python
masquerade = {
    "name": "Alice",                          # Original display name
    "avatar": "autumn-file-id-for-avatar",    # Uploaded once, cached
    "colour": "#5865F2",                      # British spelling required
}
```

- `Masquerade` permission (bit 28) is required for `name` and `avatar`
- `ManageRole` permission (bit 3) is additionally required for `colour`

Avatar upload is lazy: the first message from an author triggers an Autumn upload; all
subsequent messages reuse the cached file ID from `state.avatar_cache`.

### Idempotency-Key Deduplication

Every message send includes an `Idempotency-Key` HTTP header set to `ferry-{discord_msg_id}`.
If the same key is submitted twice (e.g. after resume), Stoat returns the existing message
rather than creating a duplicate. This makes the MESSAGES phase safe to re-run.

### Reply Reference Resolution

When `type == "Reply"`, the `reference.messageId` is looked up in `state.message_map` to get
the corresponding Stoat message ID. If the referenced message was not migrated (e.g. predates
the export), the reply is sent as a regular message and a warning is logged.

### Message Type Handling

| Type | Action |
|------|--------|
| `"Default"` | Import normally |
| `"Reply"` | Import with reply reference |
| `"ChannelPinnedMessage"` | Import, schedule re-pinning |
| `"ThreadStarterMessage"` | Import as first message in flattened thread |
| `"RecipientAdd"`, `"RecipientRemove"` | Skip |
| `"ChannelNameChange"`, `"ChannelIconChange"` | Skip |
| `"GuildMemberJoin"` | Skip (system noise) |
| `"UserPremiumGuildSubscription"` | Skip (boost notification) |
| `"ThreadCreated"` | Skip (Ferry injects its own thread header) |
| `"Call"` | Skip |

Unknown types are logged as warnings and skipped.

### Thread Headers

When processing a flattened thread channel, Ferry injects a system message at the start:
`[Thread migrated from #parent-channel]` (or `[Forum post migrated from #parent-forum]` for
forum/media threads).

---

## State and Resume

### state.json

Written to `config.output_dir/state.json` after each phase completes. Contains the full
`MigrationState` as JSON.

### Phase-Level Resume

On `--resume`, the engine loads `state.json` and compares `state.current_phase` against the
phase order list. Any phase with an index less than the saved phase is skipped (already
completed).

### Message-Level Resume

The MESSAGES phase has finer granularity (v2.0.0+):

- `state.completed_channel_ids`: set of Discord channel IDs whose messages were fully sent
- `state.channel_message_offsets`: maps a partially-processed channel ID to the last Discord
  message ID successfully sent — the phase resumes within that channel from that offset
- Messages already sent are additionally deduplicated by `Idempotency-Key` header

**v1→v2 automatic migration**: On `--resume` with a v1 state file, Ferry detects the old
`last_completed_channel` / `last_completed_message` fields, converts them to the new set/dict
format, and saves a backup of the original state file before proceeding.

### Dry-Run Rejection

`state.is_dry_run` is set to `True` during dry-run mode. If a user attempts `--resume` on a
dry-run state file, the engine raises `StateError` — you cannot resume a dry run into a real
migration.

### Atomic Saves

`save_state()` writes to a temporary file first, then renames. This prevents corrupt state
files if the process is killed mid-write.

---

## Async Patterns

### Single Shared Session

The engine creates one `aiohttp.ClientSession` before any phases run and stores it in
`config.session`. All phases reuse this session for connection pooling. The session is closed
in a `finally` block after all phases complete.

### Pause and Cancel

Two `asyncio.Event` objects on `FerryConfig` control flow:

- **`pause_event`**: Created by the GUI and starts unset. The engine waits on it at the review
  gate. The GUI sets it when the user clicks Proceed. In CLI mode, this is `None` (no blocking).
- **`cancel_event`**: Created by both GUI and CLI. Starts unset. The engine checks
  `cancel_event.is_set()` between phases. If set, it saves state and raises `MigrationError`.
  The message rate limiter also checks cancel during its sleep.

### Rate Limiting Strategy

Three layers:

1. **HTTP-level retry**: `_api_request()` handles 429 responses by sleeping for the
   server-specified `retry_after` duration, then retrying (up to 3 times)
2. **Adaptive 429-frequency optimizer**: A rolling 60-second window tracks 429 frequency.
   On a 429 burst the delay multiplier increases by 1.5×; when the window is clear it decays
   by 0.75×. This compensates for the absence of `X-RateLimit-*` response headers.
3. **Phase-level pacing**: Structure creation phases (ROLES, CATEGORIES, CHANNELS, EMOJI) add
   explicit `asyncio.sleep()` between API calls to stay within the 5/10s `/servers` bucket
4. **User-configurable delay**: `config.message_rate_limit` (default 1.0s) adds a sleep between
   each message send as a safety margin above the 10/10s message bucket

---

## Error Handling

### Exception Hierarchy

```
FerryError (base)
├── ValidationError            # Export validation failed (red status)
├── StoatConnectionError       # API unreachable or token invalid
├── AutumnUploadError          # File upload to Autumn failed
├── MigrationError             # Generic phase failure
├── StateError                 # state.json read/write problem
└── ExportError (→ MigrationError)
    ├── DCENotFoundError       # DCE binary not available
    ├── DotNetMissingError     # .NET 8 runtime not detected
    └── DiscordAuthError       # Discord token invalid or expired
```

### Phase-Level Error Handling

Each phase catches exceptions internally and:

1. Appends a structured dict to `state.errors`: `{"phase": "...", "type": "...", "error": "..."}`
2. Saves state to disk (crash recovery)
3. Re-raises the exception to the engine

The engine catches `FerryError` at the top level, saves final state, and returns the
`MigrationState` to the shell for display.

### Warnings vs Errors

- **Warnings** (`state.warnings`): Non-fatal. Migration continues. Examples: failed attachment
  upload, animated emoji (animation lost), missing sticker image.
- **Errors** (`state.errors`): Fatal. Migration stops. Examples: API unreachable, token expired,
  state file corrupt.

Both use structured dicts with `phase`, `type`, and `message` fields for filtering and
reporting.

---

## Presentation Layers

### GUI (`gui.py`) — NiceGUI

**Architecture**: NiceGUI runs an embedded FastAPI server with Vue.js frontend. Ferry launches
it in native mode (pywebview window) if available, otherwise opens the default browser to
`http://localhost:8765`.

**4-Screen Workflow**:

1. **Setup**: Credential inputs, mode toggle (1-Click vs Offline), advanced options (rate limit,
   skip flags, dry run, server ID). All fields persist to `app.storage.user`.
2. **Export** (1-Click only): DCE download progress, per-channel export progress, cached export
   detection with Use Cached / Re-export choice.
3. **Validate**: Export summary table, warnings list, ETA estimate, green/amber/red status.
4. **Migrate**: Phase indicator chips, per-channel progress bar, running totals (messages,
   attachments, errors), scrolling log, pause/resume button, cancel button with confirmation.

**Event subscription**: Passes a callback to `run_migration()` that updates NiceGUI elements.
Uses `ui.timer` for async event loop integration.

**Storage**: `app.storage.user` writes to `.nicegui/storage-user.json`. Sensitive data
(Discord tokens) is cleared in a `finally` block.

### CLI (`cli.py`) — Click + Rich

**Commands**:

| Command | Purpose |
|---------|---------|
| `ferry migrate` | Run full migration (orchestrated or offline) |
| `ferry validate` | Pre-check exports without migrating |
| `ferry build` | Create server from template or blueprint |
| `ferry export-blueprint` | Convert DCE export to reusable blueprint JSON |

**Event subscription**: `_ProgressTracker` class wraps Rich's `Live` display with a progress
bar, status table, and warning/error output. Subscribes to the same `on_event` callback as
the GUI.

**Environment variables**: `STOAT_URL`, `STOAT_TOKEN`, `DISCORD_TOKEN`, `DISCORD_SERVER_ID`.
Loaded from `.env` via python-dotenv.

### Adding a New UI

To add a new interface (e.g. REST API, TUI, Electron wrapper):

1. Create a new entry point that constructs a `FerryConfig`
2. Write an `on_event` callback that handles `MigrationEvent` instances
3. Call `await run_migration(config, on_event)`

No engine changes required. The event system is the only integration point.

---

## Exporter Module (`exporter/`)

### Binary Management (`manager.py`)

DCE (DiscordChatExporter) is an external .NET tool. Ferry downloads and caches it automatically.

- **Version**: Pinned to `DCE_VERSION = "2.47.1"`
- **Cache location**: `~/.discord-ferry/bin/dce/{version}/`
- **Platform detection**: Maps `(platform.system(), platform.machine())` to DCE release asset
  names (win-x64, linux-x64, osx-x64, osx-arm64)
- **Download**: Fetches ZIP from GitHub Releases, extracts, validates
- **.NET requirement**: macOS and Linux require .NET 8 runtime. `detect_dotnet()` checks for
  `dotnet` in PATH and validates version. Windows DCE builds are self-contained.
- **Retry**: `download_dce()` retries once on network error before raising `DCENotFoundError`

### Subprocess Execution (`runner.py`)

Runs DCE as an async subprocess with progress parsing:

```python
async def run_dce_export(config, on_event) -> None
```

- Launches `DiscordChatExporter.Cli exportguild` with the required flags
- Parses DCE's stdout for per-channel progress updates
- Emits `MigrationEvent` for each channel exported
- Raises `ExportError` on non-zero exit code

---

## Blueprint System (`blueprint.py`, `templates/`)

### Blueprints

A `ServerBlueprint` captures server structure (roles, categories, channels) without messages
or IDs. Useful for:

- Exporting a migration's structure as a reusable template
- Applying the same server layout to multiple Stoat instances

```bash
# Export blueprint from DCE exports
ferry export-blueprint --from ~/exports/my-server/ --output my-server.json

# Create server from blueprint
ferry build --blueprint my-server.json --stoat-url ... --token ...
```

### Preset Templates

Three built-in templates in `templates/`:

| Template | Roles | Categories | Channels |
|----------|-------|-----------|----------|
| `gaming` | Admin, Moderator, Member | General, Voice, Gaming | general, announcements, game-chat, voice |
| `community` | Admin, Moderator, Helper, Member | Welcome, General, Voice | welcome, rules, general, help, voice |
| `education` | Instructor, TA, Student | Announcements, Coursework, Discussion | syllabus, assignments, q-and-a, office-hours |

```bash
ferry build --template gaming --stoat-url ... --token ...
```

---

## Design Decisions

### Why One Engine, Not Per-Phase Executables?

Phases share state (ID maps, caches, pending lists). Running them as separate processes would
require serialising and deserialising state between each step, adding complexity without
benefit. A single async engine with phase functions is simpler and faster.

### Why Streaming Parser?

DCE exports for large servers can be hundreds of megabytes. Loading all messages into memory
at once would crash on modest hardware. The streaming parser (`ijson.items`) processes messages
one at a time with O(1) memory. The trade-off is slightly more complex code paths (phases must
call `stream_messages()` when `metadata_only=True`).

### Why Masquerade + Idempotency-Key?

Stoat has no bulk message import API. Every message must be sent individually via the Ferry
account. Masquerade makes each message display the original Discord author's name and avatar,
preserving conversation readability. The `Idempotency-Key` header (`ferry-{discord_msg_id}`)
prevents duplicate messages on resume — Stoat returns the existing message if the same key
was already used.

### Why Separate discord_metadata.json?

Discord permission data comes from the live Discord API, not from DCE exports. Storing it in
`state.json` would mix "what we discovered from Discord" with "what we created on Stoat."
A separate file keeps concerns clean and survives state file corruption independently.

### Why Separate Category Management?

This is a Stoat API constraint, not a design choice. Categories in Stoat are a property of
the server object (an array of `{id, title, channels[]}`), not a property of channels. There
is no `category_id` parameter on channel creation. Ferry creates all channels first, then
sends a single `PATCH /servers/{id}` with the full categories array.

### Why No ADMINISTRATOR Permission?

Stoat does not have one. There is no equivalent to Discord's bit 3 that grants all permissions.
Ferry must grant each permission individually. When translating Discord roles, ADMINISTRATOR is
expanded to all individual Stoat permission bits.

### Why Not Use stoat.py SDK?

The project originally depended on stoat.py but replaced it with a custom aiohttp wrapper
(`migrator/api.py`) in v0.9.0. The custom wrapper provides exact control over retry logic,
rate limit handling, and British spelling conventions without depending on SDK release cycles.

---

## Limits Reference

| Resource | Stoat Default | Self-Hosted Configurable |
|----------|--------------|------------------------|
| Channels per server | 200 | `server_channels` in Revolt.overrides.toml |
| Roles per server | 200 | — |
| Custom emoji per server | 100 | `server_emoji` |
| Message length | 2,000 chars | `message_length` |
| Attachments per message | 5 | — |
| Embeds per message | 5 | — |
| Reactions per message | 20 | — |
| Attachment upload size | 20 MB | `attachment_size` |

Ferry's VALIDATE phase warns when source data is likely to exceed these limits. Pass
`--max-channels N` and `--max-emoji N` to match your self-hosted configuration.

---

## Message Splitting (`migrator/messages.py`)

Messages that exceed the 2,000-character Stoat limit are split into multiple sends by
`_split_message()`:

- Splits on sentence/word boundaries where possible
- Each part carries a `[continued K/N]` marker appended to the content (budget-aware: the
  marker length is subtracted from the available character budget before generation)
- All parts share the same masquerade and are sent in order with a small inter-part delay
- The Stoat message ID of the first part is recorded in `message_map` for reply linking

---

## Thread Strategies (`migrator/messages.py`, `migrator/structure.py`)

The `thread_strategy` config option controls how forum and thread channels are handled:

| Strategy | Behaviour |
|----------|-----------|
| `flatten` | Each thread becomes its own standalone text channel (default pre-v2) |
| `merge` | Thread messages are appended into the parent channel after it is processed; threads are NOT created as separate channels. Processed after all parent channels complete. |
| `archive` | Threads are created as separate channels placed in a dedicated archive category |

`min_thread_messages` (default 0 = include all) filters out threads with fewer messages than
the threshold — useful for suppressing near-empty auto-created threads.

---

## Migration Lock (`core/engine.py`, `core/security.py`)

Ferry places an **advisory lock** in the target Stoat server's description field using the
marker `[FERRY_LOCK:{timestamp}:{hostname}]`. This prevents two Ferry instances from
migrating into the same server simultaneously.

- Lock is set at the start of the SERVER phase and cleared on completion or error
- Locks older than 24 hours are considered stale
- `--force-unlock` removes a stale lock without starting a migration

Using the server description (not a dedicated channel) avoids consuming one of the 200-channel
limit.

---

## Fidelity Scoring (`reporter.py`)

The post-migration report includes a **fidelity score**: a 0–100 weighted score across five
categories:

| Category | Weight | What is measured |
|----------|--------|-----------------|
| Messages | 40% | Messages sent vs. total, failed message rate |
| Attachments | 20% | Attachments uploaded vs. total |
| Embeds | 15% | Embeds sent vs. dropped |
| Reactions | 15% | Reactions applied (native mode only) |
| Replies | 10% | Reply links resolved vs. total replies |

The score is written to `migration_report.json` under `fidelity_score`.

---

## Incremental / Delta Migration (`core/engine.py`)

`--incremental` mode re-uses an existing state file's ID maps to migrate only new content:

- `channel_map`, `role_map`, `category_map`, `emoji_map`, `message_map`, and `avatar_cache`
  are carried forward from the prior state
- Only channels whose Discord Snowflake ID is absent from `completed_channel_ids` are processed
- Within a partially-migrated channel, `channel_message_offsets` marks the resume point
- `--resume` and `--incremental` are mutually exclusive (enforced at CLI startup)

---

## SecureTokenStore (`core/security.py`)

`SecureTokenStore` centralises token masking and sanitization at all output boundaries:

- Tokens are stored once and never appear in log lines, event messages, or error strings
- `sanitize(text)` replaces any stored token substring with `[REDACTED]` before the text
  reaches a shell, logger, or state file
- Used by the engine, CLI, and GUI before emitting any `MigrationEvent.message` that could
  contain a request URL or header value

---

## Testing

701 tests across 31 files. Key patterns:

- **Phase tests**: Mock `aiohttp.ClientSession` with `aioresponses`, inject via `config.session`
- **Parser tests**: Use fixture JSON files in `tests/fixtures/`
- **Transform tests**: Pure function tests with edge cases (code blocks, nested formatting)
- **Engine tests**: Inject mock phase functions via `phase_overrides` dict
- **CLI tests**: Click's `CliRunner` with mocked engine
- **GUI tests**: Test helper functions (ETA calculation, formatting); integration tests for
  cancel/pause behaviour

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=discord_ferry

# Run specific module
uv run pytest tests/test_messages.py -v
```
