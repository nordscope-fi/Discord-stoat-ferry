# CLI Reference

Ferry's command-line interface provides the same migration capability as the GUI, without a browser. It is useful for running unattended overnight migrations, scripting, or running on a remote server.

!!! info "Prerequisites"
    The CLI is included in the same `ferry` binary as the GUI. Run `ferry --help` to confirm it is working.

---

## Commands

Ferry has two top-level commands: `migrate` and `validate`.

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
ferry migrate EXPORT_DIR [OPTIONS]
```

`EXPORT_DIR` is the path to your DCE export folder.

### Options

| Flag | Environment Variable | Default | Description |
|------|----------------------|---------|-------------|
| `--stoat-url TEXT` | `STOAT_URL` | *(required)* | Stoat API base URL (e.g. `https://api.stoat.chat`) |
| `--token TEXT` | `STOAT_TOKEN` | *(required)* | Your Stoat user token |
| `--server-id TEXT` | | | Migrate into an existing Stoat server by ID |
| `--server-name TEXT` | | | Name for the new server (defaults to the Discord server name) |
| `--skip-messages` | | false | Import structure only — no messages sent |
| `--skip-emoji` | | false | Do not upload custom emoji |
| `--skip-reactions` | | false | Do not add reactions |
| `--skip-threads` | | false | Do not migrate threads or forum posts |
| `--rate-limit FLOAT` | | 1.0 | Seconds between messages (0.5–3.0 recommended) |
| `--upload-delay FLOAT` | | 0.5 | Seconds between Autumn file uploads |
| `--output-dir TEXT` | | `./ferry-output` | Directory for the migration report and state file |
| `--resume` | | false | Resume an interrupted migration using the saved state file |
| `--dry-run` | | false | Run all phases without making API calls; produces synthetic IDs for validation |
| `--max-channels INT` | | 200 | Channel limit; raise for self-hosted instances with custom limits |
| `--max-emoji INT` | | 100 | Emoji limit; raise for self-hosted instances with custom limits |
| `--verbose` / `-v` | | false | Enable debug output (per-message logging) |

!!! warning "Token security"
    Avoid passing `--token` directly on the command line — it may appear in shell history. Use the `STOAT_TOKEN` environment variable or a `.env` file instead.

### Environment Variables

You can set `STOAT_URL` and `STOAT_TOKEN` in a `.env` file in your working directory. Ferry loads this file automatically.

```dotenv title=".env"
STOAT_URL=https://api.stoat.chat
STOAT_TOKEN=your_token_here
```

!!! tip
    Add `.env` to your `.gitignore` if you keep your project under version control.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Migration completed successfully |
| `1` | An error occurred (details in the log) |
| `130` | Interrupted by Ctrl+C |

You can use these in scripts:

```bash
ferry migrate ~/exports/my-server/ && echo "Migration complete!"
```

---

## Examples

**Validate an export before migrating:**

```bash
ferry validate ~/exports/my-discord-server/
```

**Run a full migration using environment variables for credentials:**

```bash
export STOAT_URL=https://api.stoat.chat
export STOAT_TOKEN=your_token_here
ferry migrate ~/exports/my-discord-server/
```

**Migrate into an existing Stoat server:**

```bash
ferry migrate ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --server-id 01ABCDEF234567890ABCDEFGH
```

**Import structure only (no messages), useful for a test run:**

```bash
ferry migrate ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --skip-messages \
  --skip-emoji \
  --skip-reactions
```

**Validate the full migration pipeline without making any API calls:**

```bash
ferry migrate ./export --stoat-url https://api.stoat.chat --token "$TOKEN" --dry-run
```

**Resume an interrupted migration:**

```bash
ferry migrate ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --resume
```

**Run with a faster rate (use with caution on the official hosted service):**

```bash
ferry migrate ~/exports/my-discord-server/ \
  --stoat-url https://stoat.example.com \
  --token your_token_here \
  --rate-limit 0.5
```

!!! info "Verbose mode"
    Add `-v` or `--verbose` to any `migrate` command to see a line of output for every message sent. This is useful for diagnosing problems but produces a large amount of output for large servers.
