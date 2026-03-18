# Timestamp Preservation

When you migrate a Discord server to Stoat, you will notice that every message shows the time it was imported rather than the time it was originally posted on Discord. This page explains why this happens, how Ferry preserves the original timestamps, and an advanced workaround for self-hosted admins who want true timestamp restoration.

---

## Why Timestamps Change

Stoat uses [ULIDs](https://github.com/ulid/spec) as message IDs. A ULID encodes the creation time in its first 48 bits — when Stoat creates a message, the current server time is baked into the ID automatically. There is no API parameter to override this.

During migration, Ferry sends messages one at a time through the Stoat API. Each message receives a ULID reflecting the moment it was created on the Stoat server, not the moment it was originally posted on Discord. This is a Stoat platform limitation, not a Discord Ferry limitation.

---

## How Ferry Preserves Context

To keep the original timeline visible, Ferry prepends every migrated message with its original Discord timestamp:

```
*[2024-01-15 12:00 UTC]*
```

This prefix is italic markdown, visible to all users in any Stoat client. Messages maintain their original chronological order within each channel — they are sent in the same sequence they appeared on Discord. The prefix uses UTC to avoid timezone ambiguity across communities.

---

## What Migrated Messages Look Like

**On Discord (original):**

```
JaneDoe                       15 Jan 2024 at 12:00 PM
Hey everyone, welcome to the server!
```

**On Stoat (after migration):**

```
JaneDoe                       just now
*[2024-01-15 12:00 UTC]* Hey everyone, welcome to the server!
```

The author name and avatar are preserved through Stoat's masquerade feature — each message appears under the original Discord author's display name and profile picture. The "just now" timestamp shown by the Stoat client is the migration time, not the original posting time. The italic prefix is the only reliable indicator of when the message was actually written.

---

## Self-Hosted Workaround: True Timestamp Preservation

!!! warning "Advanced — unsupported"
    This section is for admins who run their own Stoat instance and are comfortable working directly with MongoDB. This approach bypasses the Stoat API entirely. It is not officially supported by the Stoat project and carries real risk of data corruption. **Always back up your database before attempting this.**

If you operate your own Stoat instance and need messages to display their original timestamps natively (without the italic prefix), you can insert messages directly into the database with custom ULIDs.

### Step 1: Generate ULIDs with original timestamps

The first 48 bits of a ULID encode milliseconds since Unix epoch (1 January 1970). Instead of using the current time, generate ULIDs using each message's original Discord timestamp. Libraries like `ulid-py` (Python) or `ulid` (JavaScript) accept a custom timestamp parameter.

```python
import ulid
from datetime import datetime, timezone

# Original Discord message timestamp
original_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# Generate a ULID that encodes the original timestamp
message_id = ulid.from_datetime(original_time)
```

### Step 2: Insert directly into MongoDB

Insert each message into the `messages` collection using the custom ULID as the document `_id`. The minimum required fields are:

| Field | Type | Description |
|-------|------|-------------|
| `_id` | string | Your custom ULID |
| `channel` | string | The Stoat channel ID the message belongs to |
| `author` | string | The Stoat user ID that "sent" the message (the ferry account) |
| `content` | string | The message text |
| `masquerade` | object | `{"name": "AuthorName", "avatar": "https://autumn-url/...", "colour": "#hex"}` |

```javascript
db.messages.insertOne({
    _id: "01HM3VWXYZ...",  // custom ULID from step 1
    channel: "01HXXXXXXXXX",
    author: "01HYYYYYYYYY",
    content: "Hey everyone, welcome to the server!",
    masquerade: {
        name: "JaneDoe",
        avatar: "https://your-autumn-instance/avatars/abc123",
        colour: "#7289da"
    }
});
```

### Step 3: Maintain referential integrity

Stoat does not validate foreign keys on direct database inserts, but clients will break if referenced documents are missing. Make sure:

- The `channel` value references an existing channel document.
- The `author` value references an existing user document.
- Any `avatar` URL in the masquerade object points to a file that actually exists in Autumn.

### Step 4: Notify real-time clients via Redis

After inserting a message, publish an event to Redis so that any connected clients see the new message immediately:

```
PUBLISH <channel_id> {"type": "Message", "id": "<your_custom_ulid>"}
```

Without this step, messages will appear in the database but connected clients will not see them until they reload.

### Risks and limitations

!!! danger "Read this before proceeding"
    - This approach **bypasses all API validation**. Malformed fields will not be caught on insert — they will surface as broken messages or client errors later.
    - There is a **real risk of data corruption** if documents are structured incorrectly.
    - The Stoat project provides **no official support** for direct database manipulation. You are on your own.
    - **Future Stoat updates may change the database schema**, breaking any tooling you build around direct inserts.
    - **Always back up MongoDB** before attempting this. Use `mongodump` to create a snapshot you can restore with `mongorestore`.
    - **Test on a staging instance first.** Set up a throwaway Stoat deployment, run the process there, and verify that messages display correctly before touching your production database.
