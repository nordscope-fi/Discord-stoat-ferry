# CLI Reference

Ferry's command-line interface provides the same migration capability as the GUI, without a browser. It is useful for running unattended overnight migrations, scripting, or running on a remote server.

!!! info "Prerequisites"
    The CLI is included in the same `ferry` binary as the GUI. Run `ferry --help` to confirm it is working.

---

## Commands

Ferry has four top-level commands: `migrate`, `validate`, `build`, and `export-blueprint`.

---

## `ferry validate`

Parse a DiscordChatExporter export and report what was found. Makes **zero network calls** — nothing is sent to your Stoat server.

```
ferry validate EXPORT_DIR
```

`EXPORT_DIR` is the path to the folder containing your DCE `.json` files.

**Output includes:**

- Source server name and export date
- Counts: channels, categories, roles, messages, attachments, emoji, threads
- Warnings (for example, missing media files or rendered markdown detected)
- An estimated migration time at the default 1.0s rate limit

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--rate-limit FLOAT` | 1.0 | Seconds per message for ETA calculation |

Use this command to check your export before committing to a full migration.

**Example:**

```bash
ferry validate ~/exports/my-discord-server/
```

---

## `ferry migrate`

Run the full migration. Creates or connects to a Stoat server, then imports structure and messages.

```
ferry migrate [OPTIONS]
```

!!! info "Mode selection"
    Provide either `--discord-token` + `--discord-server` (orchestrated mode) or `--export-dir` (offline mode). You cannot use both.

### Options

| Flag | Environment Variable | Default | Description |
|------|----------------------|---------|-------------|
| `--discord-token TEXT` | `DISCORD_TOKEN` | | Discord user token (orchestrated mode) |
| `--discord-server TEXT` | `DISCORD_SERVER_ID` | | Discord server ID (orchestrated mode) |
| `--export-dir PATH` | | | Path to DCE exports (offline mode) |
| `--stoat-url TEXT` | `STOAT_URL` | *(required)* | Stoat API base URL (e.g. `https://api.stoat.chat`) |
| `--token TEXT` | `STOAT_TOKEN` | *(required)* | Your Stoat user token (copied from your browser — see [setup guide](../getting-started/setup-stoat.md#2-get-your-stoat-user-token)) |
| `--server-id TEXT` | | | Migrate into an existing Stoat server by ID |
| `--server-name TEXT` | | | Name for the new server (defaults to the Discord server name) |
| `--skip-messages` | | false | Import structure only — no messages sent |
| `--skip-emoji` | | false | Do not upload custom emoji |
| `--skip-reactions` | | false | Do not add reactions |
| `--skip-threads` | | false | Do not migrate threads or forum posts |
| `--thread-strategy TEXT` | | `flatten` | Thread handling: `flatten` (each thread becomes a channel), `merge` (thread messages merged into parent channel), or `archive` (exported as markdown attachment) |
| `--rate-limit FLOAT` | | 1.0 | Seconds between messages (0.5–3.0 recommended) |
| `--upload-delay FLOAT` | | 0.5 | Seconds between Autumn file uploads |
| `--output-dir TEXT` | | `./ferry-output` | Directory for the migration report and state file |
| `--resume` | | false | Resume an interrupted migration using the saved state file |
| `--incremental` | | false | Delta migration — only migrate messages newer than the last completed run per channel. Cannot be combined with `--resume`. |
| `--force` | | false | Override DCE export freshness errors (>30 days old) and other soft warnings |
| `--dry-run` | | false | Run all phases without making API calls; produces synthetic IDs for validation |
| `--max-channels INT` | | 200 | Channel limit; raise for self-hosted instances with custom limits |
| `--max-emoji INT` | | 100 | Emoji limit; raise for self-hosted instances with custom limits |
| `--max-concurrent-channels INT` | | 3 | Number of channels to process in parallel during the message migration phase |
| `--verify-uploads` | | false | Post-upload file size verification for Autumn uploads |
| `--cleanup-orphans` | | false | Detect and report unreferenced Autumn uploads after migration (report-only; no files are deleted) |
| `--force-unlock` | | false | Override a stale migration lock on the target Stoat server |
| `--skip-dce-verify` | | false | Skip SHA-256 verification of DCE binary downloads (for self-built binaries) |
| `--verbose` / `-v` | | false | Enable debug output (per-message logging) |

!!! warning "Token security"
    Avoid passing `--token` or `--discord-token` directly on the command line — they may appear in shell history. Use environment variables or a `.env` file instead.

### Environment Variables

You can set credentials in a `.env` file in your working directory. Ferry loads this file automatically.

```dotenv title=".env"
DISCORD_TOKEN=your_discord_token_here
DISCORD_SERVER_ID=123456789012345678
STOAT_URL=https://api.stoat.chat
STOAT_TOKEN=your_stoat_token_here
```

!!! tip
    Add `.env` to your `.gitignore` if you keep your project under version control.

---

### Engine Configuration

These options are available in the migration engine but not yet exposed as CLI flags. They can be set programmatically or will be added as CLI options in a future release:

| Config Field | Default | Description |
|---|---|---|
| `skip_avatars` | False | Skip avatar pre-flight phase |
| `validate_after` | False | Run post-migration validation |
| `max_concurrent_requests` | 5 | API concurrency limit |

!!! info "More performance options"
    `--reaction-mode`, `--min-thread-messages`, `--checkpoint-interval`, and `--max-concurrent-channels` are all available as CLI flags — see the Options table above and the [large-servers guide](large-servers.md) for details.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Migration completed successfully |
| `1` | An error occurred (details in the log) |
| `130` | Interrupted by Ctrl+C |

You can use these in scripts:

```bash
ferry migrate --export-dir ~/exports/my-server/ && echo "Migration complete!"
```

---

## Examples

**1-Click migration (orchestrated):**

```bash
ferry migrate \
  --discord-token "$DISCORD_TOKEN" \
  --discord-server 123456789012345678 \
  --stoat-url https://api.stoat.chat \
  --token "$STOAT_TOKEN"
```

**Validate an export before migrating:**

```bash
ferry validate ~/exports/my-discord-server/
```

**Run a full offline migration using environment variables for credentials:**

```bash
export STOAT_URL=https://api.stoat.chat
export STOAT_TOKEN=your_token_here
ferry migrate --export-dir ~/exports/my-discord-server/
```

**Migrate into an existing Stoat server:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --server-id 01ABCDEF234567890ABCDEFGH
```

**Import structure only (no messages), useful for a test run:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --skip-messages \
  --skip-emoji \
  --skip-reactions
```

**Validate the full migration pipeline without making any API calls:**

```bash
ferry migrate --export-dir ./export --stoat-url https://api.stoat.chat --token "$TOKEN" --dry-run
```

**Resume an interrupted migration:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --resume
```

**Run with a faster rate (use with caution on the official hosted service):**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://stoat.example.com \
  --token your_token_here \
  --rate-limit 0.5
```

!!! info "Verbose mode"
    Add `-v` or `--verbose` to any `migrate` command to see a line of output for every message sent. This is useful for diagnosing problems but produces a large amount of output for large servers.

---

## `ferry build`

Create a new Stoat server from a preset template or a custom blueprint file.

```
ferry build [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `--template TEXT` | Use a preset template: `gaming`, `community`, or `education` |
| `--blueprint PATH` | Path to a custom blueprint JSON file |
| `--stoat-url TEXT` | Stoat API base URL *(required)* |
| `--token TEXT` | Your Stoat user token *(required)* |
| `--name TEXT` | Override the server name from the template/blueprint |

You must provide either `--template` or `--blueprint`, but not both.

### Preset Templates

Ferry includes three built-in server templates:

- **gaming** — Admin, Moderator, and Member roles with General, Voice, and Gaming categories
- **community** — Admin, Moderator, Helper, and Member roles with Welcome, General, and Voice categories
- **education** — Instructor, TA, and Student roles with Announcements, Coursework, and Discussion categories

Each template includes appropriate role permissions and channel structures.

**Examples:**

```bash
# Create a gaming server from a preset template
ferry build --template gaming --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"

# Create from a custom blueprint with a custom name
ferry build --blueprint my-server.json --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN" --name "My Server"
```

---

## `ferry export-blueprint`

Convert a DiscordChatExporter export directory into a reusable server blueprint JSON file. The blueprint captures server structure (roles, categories, channels) but not messages.

```
ferry export-blueprint [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `--from PATH` | Path to DCE export directory *(required)* |
| `--output PATH` | Output path for the blueprint JSON file (default: `blueprint.json`) |

**Example:**

```bash
# Export a blueprint from an existing DCE export
ferry export-blueprint --from ~/exports/my-discord-server/ --output my-server-blueprint.json

# Then use it to create a new server
ferry build --blueprint my-server-blueprint.json --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"
```

!!! tip "Blueprints use names, not IDs"
    Blueprints store role and channel names rather than Discord IDs, making them portable across different Stoat instances.
