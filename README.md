# Discord Ferry

**Migrate your Discord server to Stoat (formerly Revolt) — messages, channels, roles, emoji, attachments, and all.**

> One-click app for Windows and Mac. Command-line interface for Linux.
> No coding required. Your data stays on your machine.

---

## Download

| Platform | Download | Size |
|----------|----------|------|
| **Windows** | [Ferry.exe](https://github.com/psthubhorizon/Discord-stoat-ferry/releases/latest/download/Ferry-windows-x86_64.exe) | ~25 MB |
| **macOS** | [Ferry.zip](https://github.com/psthubhorizon/Discord-stoat-ferry/releases/latest/download/Ferry-macos-arm64.zip) | ~25 MB |
| **Linux / pip** | `pipx install discord-ferry` | ~2 MB |

---

## What is Stoat?

[Stoat](https://stoat.chat) (formerly Revolt) is an open-source chat platform — like Discord, but community-owned. You can use the official hosted service or run it on your own server. Ferry moves your entire Discord server there.

New to Stoat? [Create a free account](https://app.stoat.chat) or [self-host your own instance](docs/getting-started/setup-stoat.md).

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
| Categories | Recreated — channels grouped the same way |
| Roles | Recreated with colours and Discord permissions translated to Stoat equivalents |
| Channel permissions | Per-role and @everyone overrides migrated |
| NSFW channels | NSFW flag preserved |
| Messages + authors | Each message shows the original author's name and avatar |
| File attachments | Uploaded to Stoat's file storage |
| Custom emoji | Uploaded (up to 100) |
| Pinned messages | Re-pinned in the correct channels |
| Replies | Reply links preserved between messages |
| Reactions | Shown as text summary by default, or applied via API |
| Embeds | Flattened to Stoat format with thumbnails and images uploaded |
| Polls | Rendered as formatted text |
| Threads | Converted to text channels, merged into parent, or archived as markdown — your choice |
| Forum posts | Grouped into dedicated categories with an index channel |
| Voice channels | Created, but may not work yet (known Stoat bug) |
| Stickers | Image uploaded, or text fallback for animated/missing |
| Server banner | Uploaded from Discord API when a Discord token is provided |
| Original timestamps | Shown at the start of each message (e.g. `*[2024-01-15 12:00 UTC]*`) |

### Reliability features

Ferry is built to handle large migrations safely:

- **Pause and resume** — close Ferry anytime, pick up where you left off
- **Parallel channel sends** — processes multiple channels concurrently (3x–5x faster)
- **Incremental migration** — only migrate new messages since the last completed run
- **Pre-creation review** — summary and confirmation before anything is created on Stoat
- **Migration report** — human-readable `migration_report.md` with a fidelity score
- **Dead-letter queue** — failed messages tracked and retryable without re-running
- **Message splitting** — messages over 2000 characters are split, not truncated
- **Migration lock** — prevents two Ferry instances from targeting the same server
- **Circuit breaker** — automatic backoff on API failures, no indefinite blocking

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
