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

| Feature | Status |
|---------|--------|
| Text channels | Supported |
| Categories | Supported |
| Roles (with colours and permissions) | Supported — Discord permissions translated to Stoat equivalents |
| Channel permissions | Supported — per-role and @everyone overrides migrated |
| NSFW channels | Supported — NSFW flag set during channel creation |
| Messages + author names | Supported (each message shows the original author's name and avatar) |
| File attachments | Supported |
| Custom emoji | Supported (up to 100) |
| Pinned messages | Supported |
| Replies | Supported |
| Reactions | Supported (without per-user attribution) |
| Embeds (with media) | Supported (thumbnails and images uploaded) |
| Polls | Supported (rendered as formatted text) |
| Threads | Supported (converted to text channels) |
| Forum posts | Supported (grouped into dedicated categories) |
| Voice channels | Partial (created but do not work yet — known Stoat bug) |
| Stickers | Image upload with text fallback for Lottie/missing |
| Original timestamps | Shown in message text, not metadata |
| Pre-creation review | Summary and confirmation before anything is created on Stoat |
| Server blueprints | Export migration structure as reusable JSON templates |
| Avatar pre-flight | Uploads author avatars before message migration begins |
| CDN URL validation | Detects expired Discord attachment URLs before migration |
| Dead-letter queue | Failed messages tracked and retryable without re-running |
| Configurable reactions | Text summary (default), native API, or skip — per migration |
| Discord link rewriting | Jump links and invite URLs annotated for Stoat context |
| Circuit breaker | Exponential backoff prevents indefinite blocking on API failures |
| Post-migration validation | Verifies Stoat server matches source after migration |
| Markdown report | Human-readable `migration_report.md` generated after migration |
| Server banner migration | Supported |
| Parallel channel sends | Process multiple channels concurrently for faster migration |
| Message splitting | Messages >2000 chars automatically split with [continued] markers (not truncated) |
| Thread strategies | Flatten (default), merge into parent channel, or archive as markdown |
| Incremental migration | Only migrate new messages since last completed run |
| Migration lock | Prevents concurrent migrations to the same server |
| Fidelity scoring | Quantified migration quality score in the report |
| DCE verification | SHA-256 hash verification of DiscordChatExporter binary |
| Token security | Tokens never appear in error messages, repr output, or persisted storage |

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

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## License

MIT
