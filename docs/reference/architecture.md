# Architecture

This page explains how Discord Ferry works internally. It targets developers who want to extend the tool
and technically curious admins who want to understand what the migration process does under the hood.

---

## One Engine, Two Shells

Discord Ferry uses a strict separation between the migration engine and its user interfaces:

```
gui.py (NiceGUI) ──┐
                    ├──> core/engine.py + core/events.py
cli.py (Click)   ──┘
```

`engine.py` contains **all migration logic**. It never imports from `gui.py` or `cli.py`. The GUI and CLI
are thin shells: they configure the engine, subscribe to its event stream, and react to events by
updating the UI or printing Rich output respectively.

This design means the engine is independently testable and a new interface (e.g. a REST API or a desktop
app wrapper) can be added without touching any migration logic.

---

## Event Callback Pattern

Progress reporting is decoupled via `core/events.py`. The engine emits `MigrationEvent` dataclass
instances by calling a callback function passed in at startup.

```python
@dataclass
class MigrationEvent:
    phase: str          # e.g. "messages"
    status: str         # "started" | "progress" | "completed" | "error" | "warning" | "skipped"
    message: str        # human-readable description
    current: int        # items processed so far
    total: int          # total items in this phase
    channel_name: str   # active channel (if applicable)
    detail: dict[str, object] | None = None  # extra context for error or warning events
```

The GUI passes a callback that updates NiceGUI UI elements. The CLI passes a callback that prints
Rich progress bars. Neither interface knows anything about how the other renders events.

---

## Migration Phases

The engine runs phases in strict order. Each phase is idempotent with respect to state — if interrupted
and resumed, a completed phase is skipped entirely.

| # | Phase | What it does |
|---|-------|-------------|
| 0 | **EXPORT** | Run DiscordChatExporter to export the Discord server (skipped in offline mode) |
| 1 | **VALIDATE** | Parse all DCE JSON files, verify media was downloaded, detect format issues |
| 2 | **CONNECT** | Test Stoat API credentials, resolve Autumn upload URL, verify server accessibility (if using `--server-id`) |
| 3 | **SERVER** | Create a new Stoat server or attach to an existing one |
| 4 | **ROLES** | Create roles with colours (British spelling), set rank ordering from Discord position data, then apply Discord permissions translated to Stoat equivalents (if Discord metadata is available) |
| 5 | **CATEGORIES** | Create category structure on the server |
| 6 | **CHANNELS** | Create channels with NSFW flags, assign to categories, flatten threads to text channels, group forum threads into dedicated categories, then apply per-channel permission overrides |
| 7 | **EMOJI** | Download Discord emoji, upload to Autumn, register on Stoat server |
| 8 | **MESSAGES** | Import messages with masquerade, attachments, embeds (with media), stickers, polls, mention remapping, and reply threading |
| 9 | **REACTIONS** | Re-apply emoji reactions to migrated messages |
| 10 | **PINS** | Re-pin messages that were pinned in Discord |
| 11 | **REPORT** | Write a migration report summarising counts, skips, errors, and a post-migration checklist |

!!! note "Phase ordering"
    Phases cannot be reordered. Each phase depends on ID mappings produced by earlier phases.
    For example, MESSAGES depends on the Discord→Stoat channel map produced by CHANNELS.

!!! tip "Dry-run mode"
    Pass `--dry-run` to run all phases without making any API calls. The engine produces synthetic
    IDs for every created resource, allowing you to validate your export and configuration before
    committing to a live migration.

---

## State and Resume

`MigrationState` is a dataclass defined in `state.py`. It holds everything the engine produces and
needs across phases:

- **ID maps** — `role_map`, `channel_map`, `category_map`, `message_map`, `emoji_map` (Discord ID → Stoat ID)
- **Caches** — `avatar_cache`, `upload_cache` (local path → Autumn CDN URL), `author_names`
- **Pending** — `pending_pins`, `pending_reactions` (collected during MESSAGES, applied in later phases)
- **Logs** — `errors`, `warnings` (structured dicts with phase, context, and message)
- **Server** — `stoat_server_id`, `autumn_url`
- **Resume tracking** — `current_phase` (string), `last_completed_channel`, `last_completed_message`
- **Counters** — `attachments_uploaded`, `attachments_skipped`, `reactions_applied`, `pins_applied`
- **Timing** — `started_at`, `completed_at`
- **Flags** — `is_dry_run`

After each phase completes successfully, the engine serialises `MigrationState` to `state.json` in
the working directory.

```bash
# Resume an interrupted migration
ferry migrate --resume
```

On resume, the engine deserialises `state.json` and skips any phase whose index is less than the
index of `current_phase`. Phases that were mid-way through are re-run from the beginning of that
phase. The MESSAGES phase uses `last_completed_channel` and `last_completed_message` for
finer-grained resume; messages already sent are deduplicated via the `nonce` field — see
[Stoat API Notes](stoat-api-notes.md).

!!! warning "Do not edit state.json manually"
    The state file is an internal implementation detail. Hand-editing it can cause the engine to
    send duplicate messages or skip phases that still need to run.

---

## Key Directories

| Path | Purpose |
|------|---------|
| `src/discord_ferry/core/` | Engine (`engine.py`) and event system (`events.py`) |
| `src/discord_ferry/discord/` | Discord REST API client, permission bit translator, and metadata persistence |
| `src/discord_ferry/exporter/` | DCE binary management and subprocess execution (orchestrated mode) |
| `src/discord_ferry/parser/` | DCE JSON parsing and data model dataclasses |
| `src/discord_ferry/uploader/` | Autumn file upload client with caching |
| `src/discord_ferry/migrator/` | One module per migration phase |
| `src/discord_ferry/state.py` | `MigrationState` dataclass and JSON serialisation |
| `src/discord_ferry/config.py` | `FerryConfig` dataclass — all user-supplied settings |
| `src/discord_ferry/errors.py` | Custom exception hierarchy |
| `src/discord_ferry/parser/transforms.py` | Mention/emoji remapping, embed flattening, poll/sticker rendering |
| `src/discord_ferry/reporter.py` | Migration report generation with post-migration checklist |
| `src/discord_ferry/review.py` | Pre-creation review summary builder |
| `src/discord_ferry/blueprint.py` | Server blueprint export/import (JSON) |
| `src/discord_ferry/templates/` | Preset server templates (gaming, community, education) |
| `src/discord_ferry/gui.py` | NiceGUI shell — 4-screen workflow (Setup, Export, Validate, Migrate) |
| `src/discord_ferry/cli.py` | Click shell — `migrate`, `validate`, `build`, and `export-blueprint` commands |

The `exporter/` module has the following structure:

```
src/discord_ferry/exporter/
├── __init__.py    # Public API
├── manager.py     # DCE binary download and version management
└── runner.py      # Async subprocess execution and progress parsing
```

The `discord/` module handles Discord REST API access for permission migration:

```
src/discord_ferry/discord/
├── __init__.py       # fetch_and_translate_guild_metadata() orchestration
├── client.py         # Async HTTP client for Discord API v10
├── models.py         # DiscordRole, DiscordChannel, PermissionOverwrite dataclasses
├── metadata.py       # DiscordMetadata persistence (discord_metadata.json)
└── permissions.py    # Discord → Stoat permission bit translation
```

The `discord/` module is only active when a Discord token is available. It fetches guild
roles and channels from the Discord REST API, translates permission bitfields to Stoat
equivalents, and persists the results to `discord_metadata.json`. This file is read by
the ROLES and CHANNELS phases to apply permissions during migration.

---

## Data Flow Summary

```
Discord API (orchestrated mode only)
      │
      ├── exporter/    Download DCE binary → run export → produce DCE JSON files
      │
      └── discord/     Fetch guild roles + channels → translate permissions → save metadata
      │
      ▼
DCE JSON files + discord_metadata.json
      │
      ▼
  parser/          Parse JSON → typed dataclasses (Channel, Message, Attachment, …)
      │
      ▼
  review.py        Build pre-creation summary → show confirmation dialog/table
      │
      ▼
  engine.py        Orchestrate phases, maintain MigrationState
      │
      ├── uploader/    Download Discord media → upload to Autumn → return CDN URLs
      │
      ├── migrator/    Phase implementations — call migrator/api.py, emit MigrationEvents
      │                (ROLES and CHANNELS phases read discord_metadata.json for permissions)
      │
      └── state.py     Persist ID mappings after each phase
```

The exporter and discord modules are only active when a Discord token is provided; offline mode
starts directly at the parser step and skips permission migration. The parser is pure (no I/O
beyond reading files). The uploader is the only component that talks to Autumn. The migrator
modules are the only components that talk to the Stoat API.
