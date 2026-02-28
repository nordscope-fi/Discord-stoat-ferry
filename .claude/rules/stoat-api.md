---
globs:
  - "src/discord_ferry/migrator/**"
  - "src/discord_ferry/uploader/**"
  - "src/discord_ferry/core/**"
---

# Stoat API Rules

## Rate Limits

- **Fixed window**: 10-second reset period
- **`/servers` bucket (5/10s)**: SHARED across server create, channel create, role create, emoji create, category edit. All structure creation shares this single budget.
- **Message bucket (10/10s)**: Dedicated to `POST /channels/:id/messages`
- **Catch-all (20/10s)**: Everything else including Autumn uploads
- **Headers**: `X-RateLimit-Remaining`, `X-RateLimit-Reset-After` (ms), `X-RateLimit-Bucket`
- **429 response**: `{ "retry_after": <ms> }` — use this value for backoff
- Let stoat.py handle HTTP-level rate limits. Add configurable inter-message delay (default 1.0s) as safety margin.

## British Spelling

The Stoat API uses British English. Always use:
- `colour` (not `color`) in masquerade, embeds, roles
- `ManageCustomisation` (not `ManageCustomization`) for emoji permissions
- `Masquerade` (capital M) for the permission name

## Categories — Two-Step Process

Categories are NOT a channel property. They live on the Server object.

1. Create channel via `POST /servers/:id/channels`
2. PATCH the server's `categories` array to include the new channel ID

In stoat.py:
```python
channel = await client.http.create_server_channel(server, name=..., type=...)
await client.http.edit_category(server, category_id, channels=[...existing_ids, channel.id])
```

There is NO `category_id` parameter on channel creation.

## Masquerade

- `name` — displayed username (string)
- `avatar` — URL string (Autumn CDN URL after upload)
- `colour` — hex color string (British spelling)
- Requires `Masquerade` permission (bit 28) for name/avatar
- Requires `ManageRole` permission (bit 3) specifically for `colour`

## Message Deduplication

Always pass `nonce=f"ferry-{discord_msg_id}"` on every message send. This prevents duplicates on resume.

## Autumn File Uploads

- Autumn CANNOT accept URLs — download locally first, then upload as multipart form data
- Conservative 0.5s sleep between uploads
- Upload cache: same local path -> same Autumn file ID (avoid re-uploading)
- File size limits: attachments 20MB, avatars 4MB, icons 2.5MB, banners 6MB, emojis 500KB

## Permission Bits (Authoritative — from developers.stoat.chat)

| Name | Bit | Value |
|------|-----|-------|
| ManageChannel | 0 | 1 |
| ManageServer | 1 | 2 |
| ManagePermissions | 2 | 4 |
| ManageRole | 3 | 8 |
| ManageCustomisation | 4 | 16 |
| ViewChannel | 20 | 1048576 |
| ReadMessageHistory | 21 | 2097152 |
| SendMessage | 22 | 4194304 |
| ManageMessages | 23 | 8388608 |
| SendEmbeds | 26 | 67108864 |
| UploadFiles | 27 | 134217728 |
| Masquerade | 28 | 268435456 |
| React | 29 | 536870912 |

**There is NO single ADMINISTRATOR permission in Stoat.** Grant permissions individually.

Ferry bot minimum permissions = bits 3, 4, 20, 21, 22, 23, 26, 27, 28, 29 = `1,022,361,624`

## Server & Account Limits

- Channels per server: 200
- Roles per server: 200
- Custom emoji per server: 100
- Message length: 2,000 chars
- Attachments per message: 5
- Embeds per message: 5
- Reactions per message: 20
- New user account limits: accounts <72h old may have stricter limits
