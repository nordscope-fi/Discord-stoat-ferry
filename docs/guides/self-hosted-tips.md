# Self-Hosted Stoat Tips

This guide is for community admins who are migrating to a Stoat instance they operate themselves. Self-hosting gives you full control over limits and configuration, which makes large migrations significantly easier.

!!! info "Official hosted service"
    If you are using `api.stoat.chat` rather than your own server, skip this guide. The limits and configuration covered here only apply to self-hosted instances.

---

## Configuration Files

Self-hosted Stoat uses two configuration files:

- **`Revolt.toml`** — the main configuration file. Edit with care; most settings have sensible defaults.
- **`Revolt.overrides.toml`** — place your customizations here. Values in this file take precedence over `Revolt.toml`. Use this file so your changes survive updates.

!!! tip "Use overrides, not the main config"
    Always make your changes in `Revolt.overrides.toml`. If you edit `Revolt.toml` directly, your changes may be overwritten when you update Stoat.

---

## Raising Limits for Migration

The default limits in Stoat are designed for general community use. When importing a large Discord server, you will likely need to raise several of them.

| Setting | Default | Suggested for large migrations | Description |
|---------|---------|-------------------------------|-------------|
| `server_channels` | 200 | 500 | Maximum channels per server |
| `server_emoji` | 100 | 200 | Maximum custom emoji per server |
| `message_length` | 2000 | 4000 | Maximum characters per message |
| `attachment_size` | 20 MB | 50 MB or higher | Maximum file upload size |

Add these to your `Revolt.overrides.toml`:

```toml title="Revolt.overrides.toml"
[limits]
server_channels = 500
server_emoji = 200
message_length = 4000
attachment_size = 52428800  # 50 MB in bytes
```

Restart your Stoat instance after editing the file.

!!! warning "Restart required"
    Configuration changes do not take effect until you restart the Stoat services. Do this before starting the Ferry migration.

!!! tip "Tell Ferry about raised limits"
    After raising server limits, also pass `--max-channels 500 --max-emoji 200` to `ferry migrate` so Ferry knows to respect the higher ceiling.

!!! tip "Verify with a dry run"
    After configuring your self-hosted instance, run `ferry migrate` with `--dry-run` to validate structure mapping before committing to a full migration. This exercises all phases without making any API calls.

---

## Autumn File Storage

Autumn is Stoat's file storage service. Uploaded attachments, avatars, and emoji are stored here. Ferry uploads all media to Autumn automatically.

Autumn supports two storage backends:

- **Local filesystem** — files stored directly on the server. Simple to set up, limited by disk space.
- **S3-compatible storage** (Minio, AWS S3, Backblaze B2, etc.) — better for large migrations with many GB of media.

For migrations with 10+ GB of media, an S3-compatible backend is recommended. Check the Stoat self-hosting documentation for Autumn configuration details.

---

## Account Age

Stoat may apply stricter rate limits to accounts that were created less than 72 hours ago. Use an established account (your own personal account) rather than a freshly created bot account when running Ferry.

!!! tip "Server owner is best"
    Running Ferry with the server owner's token avoids nearly all permission-related issues. The owner automatically has all permissions on all channels.

---

## Permissions

Stoat does not have an "Administrator" permission that grants everything at once. Permissions must be granted individually. There is no shortcut.

If you are running Ferry with an account that is not the server owner, that account's role must have the following permissions on the server and on each channel:

| Permission | Required for |
|------------|-------------|
| ManageChannel | Creating and editing channels |
| ManageServer | Editing server settings and categories |
| ManagePermissions | Setting channel permission overrides |
| ManageRole | Applying masquerade colour to messages |
| ManageCustomisation | Uploading custom emoji |
| ViewChannel | Reading channels |
| ReadMessageHistory | Reading message history |
| SendMessage | Sending messages |
| ManageMessages | Pinning messages |
| SendEmbeds | Sending embed content |
| UploadFiles | Uploading attachments |
| Masquerade | Sending messages with a different display name and avatar |
| React | Adding reactions |

The simplest approach is to use the server owner's token and avoid this list entirely.

---

## Voice Channels

!!! warning "Known issue: Bug #194"
    Voice channel creation may produce text channels instead of voice channels in some Stoat versions. This is a known upstream bug. If voice channel layout matters, check your Stoat version's release notes. You can verify channel types in the Stoat web interface after migration completes.

Voice channels require the Vortex (or LiveKit) service to function. Creating voice channels without this service will create the channel structure but the channels will not be usable for voice.
