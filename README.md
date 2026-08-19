# Discord Ferry

[![CI](https://github.com/nordscope-fi/Discord-stoat-ferry/actions/workflows/ci.yml/badge.svg)](https://github.com/nordscope-fi/Discord-stoat-ferry/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/nordscope-fi/Discord-stoat-ferry)](https://github.com/nordscope-fi/Discord-stoat-ferry/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Migrate your Discord server to Stoat (formerly Revolt) — messages, channels, roles, emoji, attachments, and all.**

> One-click app for Windows and Mac. Command-line interface for Linux.
> No coding required. Your data stays on your machine.

---

## Download

| Platform | Download | Size |
|----------|----------|------|
| **Windows** | [Ferry.exe](https://github.com/nordscope-fi/Discord-stoat-ferry/releases/latest/download/Ferry-windows-x86_64.exe) | ~48 MB |
| **macOS** — Apple Silicon (M1–M5) | [Ferry.zip](https://github.com/nordscope-fi/Discord-stoat-ferry/releases/latest/download/Ferry-macos-arm64.zip) | ~48 MB |
| **macOS** — Intel | [Ferry.zip](https://github.com/nordscope-fi/Discord-stoat-ferry/releases/latest/download/Ferry-macos-x86_64.zip) | ~48 MB |
| **Linux / pip** | `pipx install discord-ferry` | ~2 MB |

> **macOS:** the first launch is blocked by Gatekeeper because Ferry is not notarized. Click **Done** — *not* "Move to Bin" — then approve it once under **System Settings → Privacy & Security → Open Anyway**. Full walkthrough in the [installation guide](https://nordscope-fi.github.io/Discord-stoat-ferry/getting-started/install/).

---

## What is Stoat?

[Stoat](https://stoat.chat) (formerly Revolt) is an open-source chat platform — like Discord, but community-owned. You can use the official hosted service or run it on your own server. Ferry moves your entire Discord server there.

New to Stoat? [Create a free account](https://stoat.chat/app) or [self-host your own instance](docs/getting-started/setup-stoat.md).

---

## How It Works

### Step 1: Enter your credentials

Launch Ferry. You'll need four things:

- **Discord user token** + **server ID** — a token is a secret key that lets Ferry access your account. Ferry shows you how to find both.
- **Stoat API URL** — the web address Ferry uses to talk to Stoat. Use `https://api.stoat.chat` for the official service, or your own domain if you run your own Stoat instance.
- **Stoat user token** — a secret key your browser saves when you log in to Stoat. No bot or app creation needed — you just copy it from your browser. The [step-by-step guide](docs/getting-started/setup-stoat.md) shows exactly where to find it.

### Step 2: Ferry exports your server automatically

Ferry downloads and runs DiscordChatExporter behind the scenes — no manual steps.

### Step 3: Click Migrate

Messages, channels, roles, emoji, and attachments migrate to Stoat.
Each message shows the original author's name and avatar. Pins are preserved.

> Already have DiscordChatExporter (DCE) exports? Ferry also supports [offline mode](docs/getting-started/export-discord.md) — just point it at your export folder.

---

## How long does it take?

Ferry processes multiple channels in parallel (configurable, default 3 concurrent). Typical throughput: ~3-5x faster than sequential. Stoat limits how fast data can be sent to protect the service, which sets the overall pace. That means:

| Messages | Estimated time |
|----------|---------------|
| 1,000 | ~6 minutes |
| 10,000 | ~1 hour |
| 100,000 | ~8-10 hours |

Ferry can **pause and resume** — close it anytime, pick up where you left off.

---

## What gets migrated?

| Discord feature | What happens |
|-----------------|-------------|
| Text channels | Recreated on Stoat with the same names and topics |
| Categories | Recreated in the same order as on Discord, with channels grouped the same way |
| Roles | All server roles recreated — even roles nobody posted with — with colours, hierarchy, hoisting ("display separately"), image icons, and Discord permissions translated to Stoat equivalents |
| Channel permissions | Per-role and @everyone overrides migrated |
| NSFW channels | NSFW flag preserved |
| Server description & NSFW flag | Copied to the new Stoat server |
| Slowmode | Per-channel slowmode settings preserved |
| Voice user limits | Per-channel user limits preserved |
| Messages + authors | Each message shows the original author's name and avatar |
| File attachments | Uploaded to Stoat's file storage |
| Custom emoji | Uploaded (up to 100 — the most-used, uploadable emoji are kept) |
| Pinned messages | Re-pinned in the correct channels |
| Replies | Reply links preserved between messages |
| Reactions | Shown as text summary by default, or applied via API (Stoat allows at most 20 reactions per message) |
| Embeds | Flattened to Stoat format with thumbnails and images uploaded |
| Polls | Rendered as formatted text |
| Threads | Converted to text channels, merged into parent, or archived as markdown — your choice |
| Forum posts | Grouped into dedicated categories with an index channel |
| Voice channels | Created, but may not work yet (known Stoat bug) |
| Stickers | Image uploaded, or text fallback for animated/missing |
| Server banner | Uploaded from Discord API when a Discord token is provided |
| Original timestamps | Shown at the start of each message (e.g. `*[2024-01-15 12:00 UTC]*`) |

> Some of these need a Discord token to read live server data: role hoisting and icons, roles nobody posted with, server description, category ordering, slowmode, and voice user limits. In offline mode (export folder only), Ferry migrates what the export contains and skips the rest with a warning.

### Reliability features

Ferry is built to handle large migrations safely:

- **Pause and resume** — close Ferry anytime, pick up where you left off
- **Parallel channel sends** — processes multiple channels concurrently (3x–5x faster)
- **Tunable performance** — concurrency, reaction mode, thread filtering, and checkpointing are all adjustable via CLI flags or the GUI's Advanced Options — see the [CLI reference](docs/guides/cli-reference.md#ferry-migrate)
- **Incremental migration** — only migrate new messages since the last completed run
- **Pre-creation review** — summary and confirmation before anything is created on Stoat
- **Migration report** — human-readable `migration_report.md` with a fidelity score
- **Invite link** — Ferry creates an invite to your new Stoat server when the migration finishes (on by default; turn off with `--no-create-invite`)
- **Failed-message recovery** — messages that fail to send are tracked and automatically re-sent on the next `--incremental` run
- **Message splitting** — messages over 2000 characters are split, not truncated
- **Migration lock** — prevents two Ferry instances from targeting the same server
- **Circuit breaker** — automatic backoff on API failures, no indefinite blocking
- **Rollback** — undo a migration with one command (or one click in the GUI) — deletes Ferry-created channels, roles, and emoji from the Stoat server
- **Post-migration stats** — `ferry stats <output-dir>` prints a console-friendly summary (entity counts, fidelity score, per-channel breakdown, error preview, elapsed time) from a completed migration — useful for support, scripting, and quick sanity checks without re-opening the report

---

## Beyond migration

Ferry ships several extra commands alongside `migrate`:

- **`ferry validate`** — check an export before migrating: counts, warnings, and a time estimate. No network calls.
- **`ferry check`** — after a migration, ask the live Stoat server whether every channel, role, category, emoji and message tail Ferry recorded is still there. Read-only.
- **`ferry repair`** — re-send messages and re-create channels or roles that `ferry check` found missing. Refuses to touch renames or a rolled-back migration.
- **`ferry retry`** — re-send the messages Ferry parked in the dead-letter queue after a failed send.
- **`ferry probe`** — diagnose a live Stoat instance (upload size limits, rate limits, voice support, webhooks). Useful for self-hosters.
- **`ferry build`** — create a fresh Stoat server from a preset template (`gaming`, `community`, or `education`) or a blueprint file.
- **`ferry export-blueprint`** — turn a Discord export into a reusable server blueprint (structure only, no messages).
- **`ferry tls-check`** — report which certificate authorities Ferry currently trusts, so you can diagnose an `unable to get local issuer certificate` error before touching proxy or antivirus settings.

See the [CLI reference](docs/guides/cli-reference.md) for all commands and options.

---

## Detailed Guides

- [Exporting from Discord manually (offline mode)](docs/getting-started/export-discord.md)
- [Setting up your Stoat instance](docs/getting-started/setup-stoat.md)
- [Your first migration (full walkthrough)](docs/getting-started/first-migration.md)
- [GUI guide (every screen explained)](docs/guides/gui-walkthrough.md)
- [CLI reference](docs/guides/cli-reference.md)
- [Migrating large servers (100k+ messages)](docs/guides/large-servers.md)
- [Self-hosted tips](docs/guides/self-hosted-tips.md)
- [Troubleshooting](docs/guides/troubleshooting.md)
- [Pre-Flight Checklist](docs/guides/pre-flight-checklist.md)
- [Known Limitations](docs/guides/known-limitations.md)
- [Timestamp Preservation](docs/guides/timestamps.md)

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## License

MIT
