# Discord Ferry

**Migrate your Discord server to Stoat (formerly Revolt) — messages, channels, roles, emoji, attachments, and all.**

One-click app for Windows and Mac. Command-line interface for Linux. No coding required.

<!-- screenshot: ferry-gui-validate-and-migrate-screens-side-by-side -->

---

## Get Started in 3 Steps

1. **[Install Ferry](getting-started/install.md)** — download the app for Windows, macOS, or Linux
2. **[Set up Stoat](getting-started/setup-stoat.md)** — find your Stoat API URL (the address Ferry uses to connect) and user token (a secret key your browser saves when you log in — no bot or app creation needed). New to Stoat? [Create a free account](https://stoat.chat/app).
3. **[Run your first migration](getting-started/first-migration.md)** — enter your Discord and Stoat credentials, click Migrate

Already have DiscordChatExporter (DCE) exports? See [Offline Migration](getting-started/export-discord.md) to skip the export step.

---

## How Long Does It Take?

Ferry processes multiple channels in parallel (configurable, default 3 concurrent). Typical throughput: ~3-5x faster than sequential. Stoat limits how fast data can be sent to protect the service, which sets the overall pace:

| Messages | Estimated time |
|----------|---------------|
| 1,000 | ~6 minutes |
| 10,000 | ~1 hour |
| 100,000 | ~8-10 hours |

Ferry can **pause and resume** — close it anytime and pick up where you left off.

---

## What Gets Migrated?

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

### Reliability Features

Ferry is built to handle large migrations safely:

- **Pause and resume** — close Ferry anytime, pick up where you left off
- **Parallel channel sends** — processes multiple channels concurrently (3x–5x faster)
- **Incremental migration** — only migrate new messages since the last completed run
- **Thread filtering** — exclude low-activity threads by minimum message count
- **Pre-creation review** — summary and confirmation before anything is created on Stoat
- **Migration report** — human-readable `migration_report.md` with a fidelity score
- **Dead-letter queue** — failed messages tracked and retryable without re-running
- **Post-migration validation** — verifies Stoat server structure matches the source
- **Message splitting** — messages over 2000 characters are split, not truncated
- **Migration lock** — prevents two Ferry instances from targeting the same server
- **Circuit breaker** — automatic backoff on API failures, no indefinite blocking
- **Server blueprints** — export your server structure as a reusable template

---

## Guides

- [GUI Walkthrough](guides/gui-walkthrough.md) — every screen explained
- [CLI Reference](guides/cli-reference.md) — all command-line options and configuration settings
- [Large Servers](guides/large-servers.md) — tips for 100k+ message migrations
- [Self-Hosted Tips](guides/self-hosted-tips.md) — raising limits, custom configuration
- [Troubleshooting](guides/troubleshooting.md) — common issues and solutions
- [Pre-Flight Checklist](guides/pre-flight-checklist.md) — verify your setup before migrating
- [Known Limitations](guides/known-limitations.md) — platform constraints and unsupported features

## Reference

- [Architecture](reference/architecture.md) — how the engine works
- [Stoat API Notes](reference/stoat-api-notes.md) — speed limits, permission mapping, and known quirks
- [DiscordChatExporter Format](reference/dce-format.md) — export file structure and field reference
