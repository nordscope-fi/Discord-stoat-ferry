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

**Stoat returns no rate limit headers.** There are no `X-RateLimit-Remaining`,
`X-RateLimit-Reset-After`, or `X-RateLimit-Bucket` headers on responses.

On a 429 response the body contains:

```json
{ "retry_after": 4200 }
```

Ferry's API client (`migrator/api.py`) sleeps for the `retry_after` value (milliseconds) and
retries. In addition, Ferry runs an **adaptive 429-frequency optimizer**: a rolling 60-second
window tracks 429 frequency and automatically adjusts a delay multiplier — increasing by 1.5× on
a burst, decaying by 0.75× when the window is clear. This reduces sustained 429s without relying
on headers. Ferry also adds a configurable inter-message delay (default 1.0 s) as a fixed safety
margin.

---

## Retry & Circuit Breaker

*Added in v1.7.0.*

All Stoat API requests go through `_api_request()` in `migrator/api.py`, which provides
automatic retry with exponential backoff and a circuit breaker for sustained failures.

### Exponential Backoff

On 5xx responses or network errors, `_api_request()` retries with a delay of
`min(2^attempt, 60) + jitter` seconds (capped at 60 seconds). This replaces the previous
fixed 2-second delay from earlier versions.

### Circuit Breaker

After **5 consecutive non-429 failures**, the client logs a warning and pauses for
**30 seconds** before retrying. The consecutive failure counter resets to 0 on any
successful request. This prevents Ferry from hammering a server that is persistently
unhealthy.

### 429 Handling

429 (Too Many Requests) responses are handled separately — the client sleeps for the
server-provided `retry_after` value and retries. **429 responses do NOT count toward
the circuit breaker threshold**, since they indicate normal rate limiting rather than
server-side failure.

### Concurrency Limit

An `asyncio.Semaphore(max_concurrent_requests)` bounds the number of in-flight API
requests. The default is 5, configurable via `FerryConfig.max_concurrent_requests`
(clamped to a minimum of 1).

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

## Categories — PATCH Server Object

Categories in Stoat live on the **Server object**, not on channels. A channel has no `category_id`
field. Categories are managed by PATCHing the server's `categories` array
(`PATCH /servers/{server_id}` with a `categories` property).

Each category in the array is an object: `{"id": <client-generated string>, "title": <max 32 chars>, "channels": [<channel_ids>]}`. Category IDs are generated client-side (e.g. using a short UUID).

**Step 1 — Build categories locally with generated IDs:**

```python
from uuid import uuid4

categories = [
    {"id": uuid4().hex[:26], "title": "General", "channels": []},
]
await api_upsert_categories(session, stoat_url, token, server_id, categories)
```

**Step 2 — After creating channels, update the categories array with channel IDs:**

```python
categories[0]["channels"] = [channel_id_1, channel_id_2]
await api_upsert_categories(session, stoat_url, token, server_id, categories)
```

There is no per-category endpoint. The entire `categories` array is written at once via the server
PATCH. Forgetting to include a channel in any category leaves it uncategorised.

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

**Ferry account minimum permission value:**

Bits 3, 4, 20, 21, 22, 23, 26, 27, 28, 29 sum to **1,022,361,624**.

```python
FERRY_PERMISSIONS = (
    8           # ManageRole       — masquerade colour
    | 16          # ManageCustomisation — emoji
    | 1_048_576   # ViewChannel
    | 2_097_152   # ReadMessageHistory
    | 4_194_304   # SendMessage
    | 8_388_608   # ManageMessages  — pins
    | 67_108_864  # SendEmbeds
    | 134_217_728 # UploadFiles
    | 268_435_456 # Masquerade
    | 536_870_912 # React
)
# == 1_022_361_624
```

---

## Masquerade

Masquerade lets Ferry post messages that appear to come from different Discord usernames and avatars,
preserving historical authorship even though all messages technically come from the Ferry account.

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

## Message Deduplication with Idempotency-Key

Every message send includes an `Idempotency-Key` HTTP header:

```python
await api_send_message(
    session, config.stoat_url, config.token, channel_id,
    content=text, idempotency_key=f"ferry-{discord_msg_id}",
)
```

If the same idempotency key is submitted twice (e.g. after an interrupted migration resumes), Stoat
returns the existing message rather than creating a duplicate. This makes the MESSAGES phase safe to
re-run.

!!! note "Deprecated: nonce body field"
    The old `nonce` body field on message sends is deprecated. Use the `Idempotency-Key` HTTP header
    instead.

---

## String Length Limits

The Stoat API enforces maximum lengths on several fields. Ferry must truncate before sending to avoid
400 errors.

| Field | Max Length | Regex | Enforced in |
|-------|-----------|-------|-------------|
| Channel name | 32 | — | `structure.py` |
| Role name | 32 | — | `structure.py` |
| Category title | 32 | — | `structure.py` |
| Masquerade name | 32 | — | `messages.py` |
| Emoji name | 32 | `^[a-z0-9_]+$` | `emoji.py` |
| Message content | 2,000 | — | `messages.py` |

Emoji names must be lowercase alphanumeric with underscores only. Names that don't match the regex
are sanitised (lowercased, invalid characters replaced with underscores) during the EMOJI phase.

---

## Emoji Creation

Custom emoji are created via `PUT /custom/emoji/{emoji_id}` with a `parent` object identifying the
owning server — **not** via `POST /servers/{id}/emojis`. The emoji ID is client-generated. The
`parent` object has `{"type": "Server", "id": "<server_id>"}`.

---

## Known Issues

!!! bug "Voice channel bug #194"
    On some self-hosted Stoat instances, creating a `VoiceChannel` via the API produces a text channel
    instead. Ferry logs a warning during the CHANNELS phase when voice channels are encountered. The
    channel is still created; it just may not behave as a voice channel on affected instances.
