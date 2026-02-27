# Stoat API Notes

This page collects everything discovered about the Stoat (formerly Revolt) API during development of
Discord Ferry. It is a practical supplement to the [official API docs](https://developers.stoat.chat),
focusing on non-obvious behaviour, gotchas, and Ferry-specific decisions.

---

## Rate Limits

Stoat uses **fixed 10-second windows** (not sliding). Key buckets:

| Bucket | Limit | Scope |
|--------|-------|-------|
| `/servers` | 5 per 10s | SHARED across server create, channel create, role create, emoji create, and category edit |
| Messages | 10 per 10s | Dedicated — `POST /channels/:id/messages` only |
| Catch-all | 20 per 10s | Everything else, including Autumn uploads |

!!! warning "The /servers bucket is shared"
    Creating a channel, a role, and an emoji in quick succession all draw from the same 5/10s budget.
    Ferry paces structure creation (ROLES, CATEGORIES, CHANNELS, EMOJI phases) to stay within this limit.

Rate limit headers on every response:

```
X-RateLimit-Remaining: 3
X-RateLimit-Reset-After: 7340   ← milliseconds until window resets
X-RateLimit-Bucket: servers
```

On a 429 response the body contains:

```json
{ "retry_after": 4200 }
```

Ferry's API client (`migrator/api.py`) handles HTTP-level rate limits automatically. Ferry adds a
configurable inter-message delay (default 1.0 s) on top of that as a safety margin to avoid
sustained bursts.

---

## British Spelling

The Stoat API uses British English in several field names. Using American spelling silently produces
incorrect or rejected requests.

| Always use | Never use |
|-----------|----------|
| `colour` | `color` |
| `ManageCustomisation` | `ManageCustomization` |

This applies to masquerade payloads, embed objects, role objects, and any other API field that carries
a color or permission name.

---

## Categories — Two-Step Process

Categories in Stoat live on the **Server object**, not on channels. A channel has no `category_id`
field. The creation flow is always two steps:

**Step 1 — Create the channel:**

```python
channel = await api_create_channel(
    session, config.stoat_url, config.token, server_id,
    name="general", channel_type="TextChannel",
)
```

**Step 2 — PATCH the server's categories array:**

```python
await api_edit_category(
    session, config.stoat_url, config.token, server_id,
    category_id, channels=[*existing_ids, channel["_id"]],
)
```

There is no shortcut. Forgetting step 2 leaves the channel uncategorised even if you intended it to
belong to a category.

---

## Permission Bits

Stoat has no single ADMINISTRATOR permission. Every capability must be granted individually via
bitmask. The authoritative list from `developers.stoat.chat`:

| Name | Bit | Value | Notes |
|------|-----|-------|-------|
| ManageChannel | 0 | 1 | |
| ManageServer | 1 | 2 | |
| ManagePermissions | 2 | 4 | |
| ManageRole | 3 | 8 | Also required for masquerade `colour` |
| ManageCustomisation | 4 | 16 | Required to create/manage emoji |
| ViewChannel | 20 | 1,048,576 | |
| ReadMessageHistory | 21 | 2,097,152 | |
| SendMessage | 22 | 4,194,304 | |
| ManageMessages | 23 | 8,388,608 | Required to pin messages |
| SendEmbeds | 26 | 67,108,864 | |
| UploadFiles | 27 | 134,217,728 | |
| Masquerade | 28 | 268,435,456 | Required for masquerade name and avatar |
| React | 29 | 536,870,912 | |

**Ferry bot minimum permission value:**

Bits 3, 20, 21, 22, 23, 26, 27, 28, 29 sum to **1,071,644,680**.

```python
FERRY_PERMISSIONS = (
    8           # ManageRole       — masquerade colour
    | 1_048_576   # ViewChannel
    | 2_097_152   # ReadMessageHistory
    | 4_194_304   # SendMessage
    | 8_388_608   # ManageMessages  — pins
    | 67_108_864  # SendEmbeds
    | 134_217_728 # UploadFiles
    | 268_435_456 # Masquerade
    | 536_870_912 # React
)
# == 1_071_644_680
```

---

## Masquerade

Masquerade lets Ferry post messages that appear to come from different Discord usernames and avatars,
preserving historical authorship even though all messages technically come from the Ferry bot.

Payload fields:

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Displayed username — the Discord author's display name |
| `avatar` | string | Autumn CDN URL of the author's avatar |
| `colour` | string | Hex colour string e.g. `"#5865F2"` — British spelling required |

Permission requirements:

- `Masquerade` (bit 28) — required for `name` and `avatar`
- `ManageRole` (bit 3) — additionally required to set `colour`

---

## Autumn File Uploads

Autumn is the Stoat media server. It cannot fetch URLs — **you must download the file locally first,
then upload it as multipart form data**.

File size limits by tag:

| Tag | Max size | Used for |
|-----|---------|---------|
| `attachments` | 20 MB | Message attachments |
| `avatars` | 4 MB | User/masquerade avatars |
| `icons` | 2.5 MB | Server icon, role icon |
| `banners` | 6 MB | Server banner |
| `emojis` | 500 KB | Custom emoji |

Ferry's uploader maintains an in-memory cache keyed on the local file path. If the same file is
encountered more than once (e.g. a user who appears as author in thousands of messages), their avatar
is uploaded to Autumn only on the first occurrence and the returned CDN URL is reused for all
subsequent messages.

A conservative 0.5 s sleep is inserted between Autumn upload requests to avoid bursting the
catch-all bucket.

---

## Server and Message Limits

| Resource | Limit |
|----------|-------|
| Channels per server | 200 |
| Roles per server | 200 |
| Custom emoji per server | 100 |
| Message length | 2,000 characters |
| Attachments per message | 5 |
| Embeds per message | 5 |
| Reactions per message | 20 |

Ferry's VALIDATE phase warns when source data is likely to exceed these limits.

---

## Message Deduplication with Nonce

Every message send includes a nonce:

```python
await api_send_message(
    session, config.stoat_url, config.token, channel_id,
    content=text, nonce=f"ferry-{discord_msg_id}",
)
```

If the same nonce is submitted twice (e.g. after an interrupted migration resumes), Stoat returns the
existing message rather than creating a duplicate. This makes the MESSAGES phase safe to re-run.

---

## Known Issues

!!! bug "Voice channel bug #194"
    On some self-hosted Stoat instances, creating a `VoiceChannel` via the API produces a text channel
    instead. Ferry logs a warning during the CHANNELS phase when voice channels are encountered. The
    channel is still created; it just may not behave as a voice channel on affected instances.
