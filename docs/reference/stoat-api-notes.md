# Stoat API Notes

This page collects everything discovered about the Stoat (formerly Revolt) API during development of
Discord Ferry. It is a practical supplement to the [official API docs](https://developers.stoat.chat),
focusing on non-obvious behaviour, gotchas, and Ferry-specific decisions.

---

## Rate Limits

Stoat uses **fixed 10-second windows** (not sliding). Buckets are chosen by
`resolve_bucket()` in `crates/delta/src/util/ratelimits.rs`:

| Bucket | Limit | Keyed by | Covers |
|--------|-------|----------|--------|
| `servers` | 5 per 10s | the server id, so **shared** | Server create and edit, channel create, role create and edit, emoji create, category PATCH |
| `channels` | 15 per 10s | **the channel id** | Everything else on an existing channel: edit, delete, and reading messages |
| `messaging` | 10 per 10s | **the channel id** | `POST /channels/:id/messages` |
| Catch-all | 20 per 10s | the route | Everything else, including Autumn uploads |

!!! warning "Two of these are per channel, and that changes what concurrency buys you"
    `channels` and `messaging` are keyed by channel id, so work spread across different channels
    does **not** share a budget. Parallelism genuinely helps there. The `servers` bucket is shared,
    so creating a channel, a role and an emoji in quick succession all draw on the same 5-per-10s
    budget however they are scheduled, and parallelism buys nothing.

!!! note "A read costs the same as a big read"
    Buckets count **requests**, not items, so asking a channel for 100 messages costs exactly what
    asking for 1 does.

**Stoat sets rate limit headers on every response.** `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset-After` and `X-RateLimit-Bucket` are attached
unconditionally in `crates/core/ratelimits/src/rocket.rs`, and exposed through CORS in
`crates/delta/src/main.rs`.

!!! warning "`X-RateLimit-Reset-After` is MILLISECONDS"
    Discord's identically named header is **seconds**. Ferry has confused the two in both
    directions: v2.8.2 read Stoat's as seconds and slept the 60-second cap on every 429, and v2.8.3
    made the opposite mistake in the Autumn client and retried nine seconds early into a bucket that
    was still closed.

    An earlier version of this page said Stoat sends no such headers. That came from revolt.js's
    binding documentation, which does not surface them. **A client binding's docs are not evidence
    about server behaviour.** Reading the backend source settled it.

On a 429 response the body also contains:

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
bitmask.

> **Read the source, not the docs site.** The authoritative list is the `ChannelPermission` enum in
> `stoatchat/stoatchat` at `crates/core/permissions/src/models/channel.rs`. `developers.stoat.chat`
> has lagged it, and an earlier revision of this page reproduced a 13-bit subset from that site —
> which is how Ferry came to drop `MentionEveryone` as "nonexistent" and to ship voice channels that
> granted no voice permissions. Verified against source 2026-08-02 (commit `502203d3`).

All **34** defined bits:

| Name | Bit | Notes |
|------|-----|-------|
| ManageChannel | 0 | |
| ManageServer | 1 | |
| ManagePermissions | 2 | |
| ManageRole | 3 | Also required for masquerade `colour` |
| ManageCustomisation | 4 | Required to create/manage emoji |
| KickMembers | 6 | |
| BanMembers | 7 | |
| TimeoutMembers | 8 | |
| AssignRoles | 9 | Split out of `ManageRole` |
| ChangeNickname | 10 | |
| ManageNicknames | 11 | |
| ChangeAvatar | 12 | No Discord analogue |
| RemoveAvatars | 13 | No Discord analogue |
| ViewChannel | 20 | |
| ReadMessageHistory | 21 | |
| SendMessage | 22 | |
| ManageMessages | 23 | Required to pin messages — Stoat has no pin-only bit |
| ManageWebhooks | 24 | |
| InviteOthers | 25 | |
| SendEmbeds | 26 | |
| UploadFiles | 27 | Send attachments and media |
| Masquerade | 28 | Required for masquerade name and avatar |
| React | 29 | |
| Connect | 30 | Join a voice channel |
| Speak | 31 | Publish microphone |
| Video | 32 | Publish camera / screen share |
| MuteMembers | 33 | |
| DeafenMembers | 34 | |
| MoveMembers | 35 | |
| Listen | 36 | **Receive** audio and video — see below |
| MentionEveryone | 37 | |
| MentionRoles | 38 | |
| BypassSlowmode | 39 | |
| ViewAuditLogs | 40 | |

Bit 5 and bits 14–19 are undefined gaps. Bits **41–52 are a declared "free area"** and 53–64 are
marked do-not-use, so never derive an "all permissions" value from Stoat's `GrantAllSafe`
(`0x000F_FFFF_FFFF_FFFF`) — it spans the free area, and Stoat computes it for server owners without
ever persisting it to a role.

**`Connect` alone does not give you a working voice channel.** Stoat gates *joining* on `Connect`
(`voice_join.rs:52`) but builds the LiveKit token's `can_subscribe` from `Listen`
(`voice_client.rs:95`), so a member with `Connect` and `Speak` but no `Listen` joins the call and
hears nobody. Discord's single `CONNECT` permission covers both, so Ferry maps it to both bits.

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

!!! danger "This does NOT make the MESSAGES phase safe to re-run"
    An earlier revision of this page claimed that submitting the same key twice makes Stoat return
    the existing message instead of creating a duplicate. **Both halves of that are wrong**, and the
    claim mattered — it was the stated reason resume was considered safe.

    Verified against `crates/core/database/src/util/idempotency.rs` (2026-08-02):

    - The store is `Lazy<Mutex<lru::LruCache<String, ()>>>` with a capacity of **1000 entries**. It
      is **in-memory, process-local, has no TTL, and is emptied whenever the server restarts.**
    - A key that is still cached returns **HTTP 409 `DuplicateNonce`** (`Status::Conflict`). It does
      **not** return the original message, so there is nothing to reconcile against.
    - Keys longer than **64 characters** are rejected. Ferry's are ~25–31 (`ferry-{id}`,
      `ferry-merge-{id}`, `_p{n}` suffix), so this is not currently a constraint.

    A 1000-entry window is a handful of seconds of migration traffic. By the time any resume, retry
    or repair runs, every relevant key is long gone — and after a server restart, immediately.

**What actually prevents duplicates is entirely client-side**, and each strategy has its own
mechanism:

| Path | What stops a re-run duplicating | Where |
|---|---|---|
| `flatten` | `state.message_map` (source id → Stoat id), plus `completed_channel_ids` and the transient `channel_message_offsets` | `messages.py` |
| `merge` | `channel_high_water` per thread (durable) and `channel_message_offsets` (transient, mid-thread). **`merge` never writes `message_map`** | `messages.py` |
| `--incremental` | the durable `channel_high_water` marker, with previously-failed ids deliberately re-attempted | `messages.py` |

Treat the idempotency key as a cheap guard against an immediate double-send within one run — a
retried request, a duplicated task — and nothing more.

### Ferry acts on the 409 as of v2.14.7

Before v2.14.7 the guard fired and Ferry treated the answer as a failure. A 409 reached the generic
error handler at every send site, which recorded the message in `state.failed_messages`. An
`--incremental` run then re-attempted it, by which point the key had left the cache, so the re-send
succeeded and the channel held the message twice. The mechanism meant to prevent a duplicate was
producing one.

A 409 whose body is `{"type": "DuplicateNonce"}` now raises `DuplicateSendError`, and every send
site treats it as delivered. Nothing is recorded as failed, so nothing re-sends it. Any other 409,
and any body that is not a JSON object, still raises the generic error.

What it costs, because the 409 carries no message id:

- A duplicate on a message's **first part** loses its `message_map` entry. Replies to that one
  message will not resolve, its pin is skipped and its reactions are skipped. A warning of type
  `duplicate_send_unmapped` names the message.
- A duplicate on a **later part** costs nothing. Only the first part feeds the map, and the
  remaining parts still send, because each part carries its own key.
- A duplicate on the **forum index** means the index message cannot be pinned. The channel wiring
  does not need that id and still happens.

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
| Channel topic / description | 1,024 | — | `structure.py`, `core/engine.py` (defence-in-depth against hand-edited exports) |

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
