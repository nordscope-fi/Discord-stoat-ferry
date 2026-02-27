# Discord Ferry

**Migrate your Discord server to Stoat (formerly Revolt) — messages, channels, roles, emoji, attachments, and all.**

One-click app for Windows and Mac. CLI for Linux. No coding required.

<!-- screenshot: ferry-gui-validate-and-migrate-screens-side-by-side -->

---

## Get Started in 3 Steps

1. **[Export your Discord server](getting-started/export-discord.md)** — use DiscordChatExporter to save your server locally
2. **[Install Ferry and connect to Stoat](getting-started/install.md)** — download the app, enter your Stoat URL and token
3. **[Run your first migration](getting-started/first-migration.md)** — click Migrate and watch it go

New to Stoat? See [Setting up Stoat](getting-started/setup-stoat.md) first.

---

## How Long Does It Take?

About **1 message per second** due to Stoat API rate limits:

| Server size | Estimated time |
|-------------|---------------|
| 1,000 messages | ~17 minutes |
| 10,000 messages | ~3 hours |
| 100,000 messages | ~28 hours |

Ferry can **pause and resume** — close it anytime and pick up where you left off.

---

## What Gets Migrated?

| Feature | Status |
|---------|--------|
| Text channels | Supported |
| Categories | Supported |
| Roles (with colours) | Supported |
| Messages + author names | Supported (via masquerade) |
| File attachments | Supported |
| Custom emoji | Supported (up to 100) |
| Pinned messages | Supported |
| Replies | Supported |
| Reactions | Supported (without per-user attribution) |
| Threads | Supported (converted to text channels) |
| Forum posts | Supported (converted to text channels) |
| Voice channels | Partial (created but may not function — Stoat bug) |
| Stickers | Text placeholder only |
| Original timestamps | Shown in message text, not metadata |

---

## Guides

- [GUI Walkthrough](guides/gui-walkthrough.md) — every screen explained
- [CLI Reference](guides/cli-reference.md) — all flags and environment variables
- [Large Servers](guides/large-servers.md) — tips for 100k+ message migrations
- [Self-Hosted Tips](guides/self-hosted-tips.md) — raising limits, custom configuration
- [Troubleshooting](guides/troubleshooting.md) — common issues and solutions

## Reference

- [Architecture](reference/architecture.md) — how the engine works
- [Stoat API Notes](reference/stoat-api-notes.md) — rate limits, permissions, quirks
- [DCE Format](reference/dce-format.md) — DiscordChatExporter JSON schema
