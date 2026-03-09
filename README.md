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
- **Stoat user token** — your Stoat secret key. The [step-by-step guide](docs/getting-started/setup-stoat.md) shows exactly where to find it.

### Step 2: Ferry exports your server automatically

Ferry downloads and runs DiscordChatExporter behind the scenes — no manual steps.

### Step 3: Click Migrate

Messages, channels, roles, emoji, and attachments migrate to Stoat.
Each message shows the original author's name and avatar. Pins are preserved.

> Already have DiscordChatExporter (DCE) exports? Ferry also supports [offline mode](docs/getting-started/export-discord.md) — just point it at your export folder.

---

## How long does it take?

About **1 message per second**. Stoat limits how fast data can be sent to protect the service, which sets this pace. That means:
- 1,000 messages ~ 17 minutes
- 10,000 messages ~ 3 hours
- 100,000 messages ~ 28 hours (run overnight!)

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

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## License

MIT
