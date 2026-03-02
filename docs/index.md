# Discord Ferry

**Migrate your Discord server to Stoat (formerly Revolt) — messages, channels, roles, emoji, attachments, and all.**

One-click app for Windows and Mac. CLI for Linux. No coding required.

<!-- screenshot: ferry-gui-validate-and-migrate-screens-side-by-side -->

---

## Get Started in 3 Steps

1. **[Install Ferry](getting-started/install.md)** — download the app for Windows, macOS, or Linux
2. **[Set up Stoat](getting-started/setup-stoat.md)** — find your Stoat API URL and user token (new to Stoat? [Create a free account](https://app.stoat.chat))
3. **[Run your first migration](getting-started/first-migration.md)** — enter your Discord and Stoat credentials, click Migrate

Already have DCE exports? See [Offline Migration](getting-started/export-discord.md) to skip the export step.

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
| Voice channels | Partial (created but may not function — Stoat bug) |
| Stickers | Image upload with text fallback for Lottie/missing |
| Original timestamps | Shown in message text, not metadata |
| Pre-creation review | Supported — summary and confirmation before anything is created on Stoat |
| Server blueprints | Supported — export migration structure as reusable JSON templates |

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
