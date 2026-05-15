# Design: replace hand-authored fixtures with captured-real (issue #35)

**Date:** 2026-05-15 (revised 2026-05-16 after critique pass)
**Issue:** [#35](https://github.com/nordscope-fi/discord-stoat-ferry/issues/35)
**Ships as:** Three sequenced sub-PRs after #23 (v2.2.0) and #36 land (see Phasing)
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Revisions from critique pass (2026-05-16)

- **Finding 1 (test server recipe coverage gaps):** Test server recipe expanded to enumerate every parser/transforms branch — system messages (GuildMemberJoin, ChannelPinnedMessage, RecipientAdd, UserPremiumGuildSubscription), polls (`flatten_poll`), stickers (both name + sourceUrl branches of `handle_stickers`), in-content custom emoji (`<:name:id>` via `remap_emoji`), user/channel/role mentions (`remap_mentions`), Discord jump links + invite links (`rewrite_discord_links`), spoilers/underline/code spans (`convert_spoilers`/`strip_underline`/`_transform_outside_code`), reply-to-deleted-message (`reference.messageId == ""` fallback), expired CDN URLs (`check_cdn_url_expiry`). See "Test server requirements" below for the full inventory mapped to source-line citations.
- **Finding 2 (bot vs user token + ToS):** **Decided: BOT TOKEN.** No more "document both options." `validate_discord_token` will be updated to accept the `Bot <token>` shape in addition to the existing user-token shape; DCE `--token <token> -b` flag is verified to accept bot tokens. See "Token decision" section.
- **Finding 3 (capture-script glue is hand-waved):** Concrete code outline added showing minimal `FerryConfig` construction, no-op `EventCallback`, `asyncio.run(...)` invocation, and tmpdir routing for `export_dir`. See "Capture-script glue (concrete outline)".
- **Finding 4 (IdMapper produces invalid snowflakes):** Redesigned to produce structurally valid Discord snowflakes — 64-bit ints with a fixed 2024-01-01 timestamp prefix in the high 42 bits; HMAC(salt, real_id) packed into the low 22 bits (5 worker + 5 process + 12 sequence). Mapping does NOT preserve sort order across distinct real IDs (documented limitation; tests must not assert ordering by ID). See "IdMapper class".
- **Finding 5 (CDN scrub list is incomplete):** Scrub list expanded to enumerate all known Discord CDN/media hosts (`cdn.discordapp.com`, `media.discordapp.net`, `images-ext-1.discordapp.net`, `images-ext-2.discordapp.net`, `cdn.discord-attachments.com`, `tenor.com`, plus user-supplied embed URLs in `embed.url` / `embed.image.url` / `embed.video.url`). Also handles URL-embedded IDs (e.g. `cdn.discordapp.com/emojis/<id>.png`) by extracting the snowflake, running it through `IdMapper`, and reassembling — so the scrubbed URL points to the same fake ID as elsewhere in the fixture. See "CDN/URL scrub".
- **Finding 6 (test rewrite scope undercounted ~50 → ~300 lines):** Confirmed: 102 asserts in `test_parser.py` + 121 in `test_transforms.py` = 223 asserts, most depend on hand-crafted IDs/names. PR phasing restructured into three sub-PRs to bound risk per merge: (a) capture infra + IdMapper + IdMapper unit tests; (b) Peter runs capture out-of-band, commits scrubbed fixtures only; (c) rewrite ~300 lines of test assertions across `test_parser.py` / `test_transforms.py` / etc. See revised "Phasing".
- **Minor — bus factor on credentials:** Salt + bot token stored in shared 1Password (or Bitwarden) vault, not just Peter's local machine. Documented in `tests/fixtures/README.md`.
- **Minor — fixture versioning across DCE bumps:** Decided HARD-CUT (single `tests/fixtures/` directory, replaced wholesale per DCE bump). Rejected side-by-side `dce-2.47.1/` / `dce-2.48.0/` directories as YAGNI; if a future bug requires a regression suite across DCE versions, we'll revisit.
- **Minor — IdMapper empty-string footgun:** Empty-string inputs (`reference.messageId == ""` for non-replies, `category_id == ""` for uncategorized channels) are whitelisted as pass-through — `map_id("")` returns `""`. Other non-snowflake inputs still raise.

## Problem

Every fixture under `tests/fixtures/` is hand-authored by the same developer in the same commits as the consumer code. At least one (`dce_stdout_sample.txt`) was provably wrong since 2026-02-28 (caused #23). At least one other (`simple_channel.json`) uses the correct schema (`isInline`) but is otherwise idealized: round timestamps, sequential IDs, no real-world noise. The same anti-pattern likely affects more.

Replace each fixture with a transcript captured from a real DCE 2.47.1 run against a dedicated test Discord server, so the fixtures are the contract definition for the parser layer.

## Architecture

### One capture script (`scripts/capture-fixtures.py`)

Python script (matches project language; reuses Ferry's own `download_dce()` and `run_dce_export()`). Steps:

1. Read configuration: test-server ID + Discord BOT token from env vars (`FERRY_TEST_SERVER_ID`, `FERRY_TEST_DISCORD_BOT_TOKEN`).
2. Invoke DCE via Ferry's existing `run_dce_export()` to export the test server (see "Capture-script glue" for the concrete code outline).
3. For each captured JSON file in the export output, run `IdMapper` to deterministically replace real Discord IDs with structurally valid fake snowflakes.
4. For each, replace user display names with `TestUser1`, `TestUser2`, etc. (also via deterministic mapping).
5. Replace CDN/media URLs across all known hosts (see "CDN/URL scrub" below); within URL paths, extract embedded IDs and re-route through `IdMapper`.
6. Write the scrubbed JSON files to `tests/fixtures/` overwriting the existing ones.
7. Also capture the DCE stdout/stderr transcript to `tests/fixtures/dce_stdout_sample.txt`.

### Token decision

**Decided: BOT TOKEN.** A user token would constitute selfbotting (Discord ToS violation). Concrete actions in the implementation PR:

- Capture environment variable is `FERRY_TEST_DISCORD_BOT_TOKEN` (NOT `FERRY_TEST_DISCORD_TOKEN`, to disambiguate from any user-flow env vars elsewhere).
- Bot is invited to the test server with the `Read Messages`, `Read Message History`, and `View Channel` permissions (minimum for a full export).
- DCE `exportguild` accepts bot tokens via `--token <token> -b` (the `-b` flag tells DCE the token is a bot token). The capture script appends `-b` to the DCE command for the capture run only — production Ferry usage is unchanged.
- `validate_discord_token` (currently `src/discord_ferry/exporter/runner.py:76-96`) is updated: instead of always sending raw `Authorization: <token>`, it detects the `Bot ` prefix and sends `Authorization: Bot <token>`; falls back to raw header for user tokens. (User-token flow is preserved because DCE itself still primarily targets user tokens; only the capture script prefers bot.)
- The bot token is stored in a shared 1Password vault entry (`stoat-ferry / fixture-capture / bot-token`) so a second maintainer can re-capture if Peter's machine is unavailable.

### Test server requirements (provisioned by user, not in scope of this PR)

A dedicated Discord test server seeded with the following content. Each row is mapped to the parser/transforms branch it covers; absence of any row means that branch ships uncovered.

**Channels and structure**

- 1 text channel "general" (uncategorized — covers `channel.categoryId == ""` pass-through in `_parse_channel` at `dce_parser.py:316`).
- 1 text channel "announcements" inside a category named "Info" (covers `categoryId` populated branch).
- 1 thread under "general" with 3 messages (covers `_THREE_SEGMENT_RE` filename inference at `dce_parser.py:30`/`290`).
- 1 forum channel "feedback" with 1 forum post + 2 replies (covers forum-thread filename inference + thread reply ordering).
- 1 voice channel (Ferry skips voice channels but DCE may emit metadata — covers the skip path).

**Message content (in "general" unless otherwise noted)**

| # | Content | Branch covered (file:lines) |
|---|---------|-----------------------------|
| 1 | Plain text "hello world" | `_parse_message` happy path (`dce_parser.py:327`) |
| 2 | Message with image attachment (~50 KB PNG) | `_parse_attachment` (`dce_parser.py:386`); `attachments` non-empty |
| 3 | Message with rich embed: title, description, 3 inline fields, 2 non-inline fields, footer, author, thumbnail | `flatten_embed` full path (`transforms.py:168`) — covers `isInline` true + false rows, `_flush_inline_row`, footer, author |
| 4 | Message with custom-emoji reaction | `_parse_reaction` (`dce_parser.py:395`); `emoji.id` non-empty branch |
| 5 | Message with unicode-emoji reaction | `_parse_reaction`; `emoji.id == ""` branch |
| 6 | Pinned message | `is_pinned == True` branch (`dce_parser.py:352`) |
| 7 | Edited message (use Discord client to edit after first send) | `timestamp_edited` non-None branch (`dce_parser.py:353`) |
| 8 | Message with in-content custom emoji `<:name:id>` (NOT a reaction) | `remap_emoji` + `_CONTENT_EMOJI_RE` (`dce_parser.py:29`, `transforms.py:108`) |
| 9 | Message with @user mention | `remap_mentions` user branch (`transforms.py:62`, regex `_USER_MENTION_RE`) |
| 10 | Message with #channel mention | `remap_mentions` channel branch |
| 11 | Message with @role mention (custom role created on test server) | `remap_mentions` role branch |
| 12 | Message with Discord jump link `https://discord.com/channels/<g>/<c>/<m>` to message #1 | `rewrite_discord_links` jump-link branch (`transforms.py:129`) |
| 13 | Message with Discord invite link `https://discord.gg/abc123` | `rewrite_discord_links` invite branch |
| 14 | Message with spoiler `\|\|secret\|\|` | `convert_spoilers` (`transforms.py:54`) |
| 15 | Message with underline `__underlined__` | `strip_underline` (`transforms.py:368`) |
| 16 | Message with inline code containing all of the above markers (spoiler, underline, mention, emoji) — proves `_transform_outside_code` skips code spans | `_transform_outside_code` (`transforms.py:23`) |
| 17 | Message with fenced code block containing the same markers | Same as #16, fenced-block path |
| 18 | Reply to a message that is then deleted (post-export the parent will be missing) | `reference.messageId == ""` fallback (`dce_parser.py:336`); also exercises the IdMapper empty-string whitelist |
| 19 | Message with a sticker that has a `name` only (no `sourceUrl`) | `handle_stickers` name-only branch (`transforms.py:337`) |
| 20 | Message with a sticker that has a `sourceUrl` pointing to a downloaded local file | `handle_stickers` sourceUrl branch with `local.exists()` |
| 21 | Message with an EXPIRED Discord CDN attachment URL (artificially constructed by including a stale `?ex=` parameter) | `check_cdn_url_expiry` returns True; `validate_export` emits `expired_cdn_url` warning (`dce_parser.py:121`, `253`) |
| 22 | Active poll: question + 3 answer options with vote counts | `flatten_poll` (`transforms.py:289`) — both `question` as dict and `answer.text` as dict variants |
| 23 | System message: GuildMemberJoin | `_parse_message` with `type == "GuildMemberJoin"`; downstream skip behavior |
| 24 | System message: ChannelPinnedMessage | `type == "ChannelPinnedMessage"` |
| 25 | System message: RecipientAdd (group-DM-style; may need a different test server type, see Open questions) | `type == "RecipientAdd"` |
| 26 | System message: UserPremiumGuildSubscription (boost) | `type == "UserPremiumGuildSubscription"` |
| 27 | Message from a bot author (the capture bot can post one to itself) | `is_bot == True` branch (`dce_parser.py:380`) |
| 28 | Message from an author with a nickname different from username | `nickname != ""` branch (`dce_parser.py:378`) |
| 29 | Message from an author with one or more roles assigned | `roles` non-empty branch (`dce_parser.py:365`) |
| 30 | Embed with a `tenor.com` GIF URL | CDN scrub branch for `tenor.com` host |
| 31 | Embed with `images-ext-1.discordapp.net` proxied image | CDN scrub branch for proxied-image host |
| 32 | Markdown-rendered message (one with `<@` raw mention syntax stripped — simulating an export taken without `--markdown false`) | `validate_export` `rendered_markdown` warning path (`dce_parser.py:194`) |

If any row is genuinely impossible to seed (e.g., RecipientAdd may only appear in group DMs, not guild channels), document the gap in `tests/fixtures/README.md` and either (a) hand-craft a minimal supplementary fixture for just that branch with a comment explaining why, or (b) add an `xfail`-style test acknowledging the uncovered branch. **Do NOT silently drop coverage.**

These rows go into `.env.example` (gitignored secrets file) for local capture; into shared 1Password for the bot token + salt.

### Capture-script glue (concrete outline)

The script reuses `run_dce_export()` from `discord_ferry.exporter.runner`. Concrete shape (approximate; final names settled in implementation):

```python
# scripts/capture-fixtures.py
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from discord_ferry.config import FerryConfig
from discord_ferry.core.events import MigrationEvent
from discord_ferry.exporter.downloader import download_dce
from discord_ferry.exporter.runner import run_dce_export, _build_dce_command

# Local helpers (same script or scripts/_id_mapper.py)
from _id_mapper import IdMapper, scrub_export_dir


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
SALT_HEX = "<32-byte hex constant committed to script>"  # see "IdMapper class"


def _silent_event(event: MigrationEvent) -> None:
    """No-op EventCallback. Capture script doesn't need GUI events."""
    # Optionally: print(event.status, event.message) for debug.


async def _run_capture(tmpdir: Path) -> Path:
    server_id = os.environ["FERRY_TEST_SERVER_ID"]
    bot_token = os.environ["FERRY_TEST_DISCORD_BOT_TOKEN"]

    config = FerryConfig(
        discord_token=bot_token,
        discord_server_id=server_id,
        export_dir=tmpdir / "export",
        # All Stoat-side fields can be left at defaults — capture script
        # only invokes the EXPORT phase, never the upload phase.
        cancel_event=None,
    )

    # Ensure DCE binary present (downloads + verifies if needed).
    dce_path = await download_dce(on_event=_silent_event)

    # Patch the command builder to append `-b` (bot-token flag) for capture.
    # Implementation: either monkey-patch _build_dce_command for the capture
    # run, OR add an opt-in `bot_token=True` kwarg to run_dce_export. Pick
    # whichever is least invasive on the production code path during
    # writing-plans. For this spec, assume a small refactor of
    # _build_dce_command to accept an optional `is_bot_token: bool = False`.
    export_dir = await run_dce_export(
        config=config,
        dce_path=dce_path,
        on_event=_silent_event,
    )
    return export_dir


def main() -> int:
    if "FERRY_TEST_SERVER_ID" not in os.environ:
        print("ERROR: FERRY_TEST_SERVER_ID not set", file=sys.stderr)
        return 2
    if "FERRY_TEST_DISCORD_BOT_TOKEN" not in os.environ:
        print("ERROR: FERRY_TEST_DISCORD_BOT_TOKEN not set", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="ferry-fixture-capture-") as tmp:
        tmpdir = Path(tmp)
        export_dir = asyncio.run(_run_capture(tmpdir))

        mapper = IdMapper(salt=bytes.fromhex(SALT_HEX))
        scrub_export_dir(export_dir, mapper)

        # Replace the committed fixtures wholesale (hard-cut versioning).
        for json_file in sorted(export_dir.glob("*.json")):
            shutil.copy(json_file, FIXTURES_DIR / json_file.name)

        # Capture DCE stdout transcript separately (see open question on stderr).
        # The transcript is written by run_dce_export's debug logger; we re-run
        # with a captured-stream wrapper, OR (simpler) we add an optional
        # `transcript_path` kwarg to run_dce_export that tee's stdout to a file.

    print(f"Captured + scrubbed {len(list(FIXTURES_DIR.glob('*.json')))} fixtures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two small Ferry-side refactors are required to make this work cleanly without monkey-patching:

1. `_build_dce_command` grows an optional `is_bot_token: bool = False` parameter. When True, it appends `-b` to the command. Default behavior unchanged.
2. `run_dce_export` grows an optional `is_bot_token: bool = False` parameter that is forwarded to `_build_dce_command`. (Alternatively: add an optional `extra_dce_args: list[str] | None = None` parameter. Pick during writing-plans; minimal-surface-change wins.)

Both refactors are additive, default-off, and don't change production behavior. They land in sub-PR (a).

### `IdMapper` class

```python
import hmac
from dataclasses import dataclass

# Discord epoch: 2015-01-01T00:00:00Z in milliseconds since Unix epoch.
DISCORD_EPOCH_MS = 1_420_070_400_000

# Fake-fixture epoch: 2024-01-01T00:00:00Z. Chosen because:
# - Far enough after Discord epoch that snowflakes look real
# - Stable across captures (does NOT use time.now())
# - Easy to spot in logs ("any 2024-01 snowflake is fake-fixture-data")
FAKE_EPOCH_MS = 1_704_067_200_000  # 2024-01-01

# Snowflake bit layout (Discord standard):
#   bits 63..22: 42-bit timestamp = (ms_since_discord_epoch)
#   bits 21..17: 5-bit worker ID
#   bits 16..12: 5-bit process ID
#   bits 11..0:  12-bit sequence
TIMESTAMP_PREFIX = (FAKE_EPOCH_MS - DISCORD_EPOCH_MS) << 22  # constant high bits


@dataclass
class IdMapper:
    """Deterministically map real Discord snowflakes to STRUCTURALLY VALID fake ones.

    Same real ID → same fake ID across all files in one capture run.
    Reproducible: rerunning against the same input produces the same output.

    The fake snowflakes:
    - Have a valid 42-bit timestamp prefix (all 2024-01-01)
    - Have valid 5+5+12 bit worker/process/sequence regions
    - Are 64-bit ints serialized as decimal strings, length 18-19
    - DO NOT preserve sort order across distinct real IDs (timestamp is constant;
      the low 22 bits come from HMAC, which is collision-resistant but not
      order-preserving). Tests must NOT assert ordering by ID. Sort by
      `timestamp` field instead.
    - Empty string `""` is whitelisted as pass-through (Discord uses ""
      for non-reply `reference.messageId` and uncategorized `categoryId`).
    """

    salt: bytes

    def map_id(self, real_id: str) -> str:
        if real_id == "":
            return ""  # whitelist: empty is a meaningful Discord sentinel
        if not real_id.isdigit():
            raise ValueError(f"map_id expected snowflake (digits) or '', got {real_id!r}")

        # Hash real ID with salt; take first 22 bits (low region of snowflake).
        digest = hmac.new(self.salt, real_id.encode("ascii"), "sha256").digest()
        low_22 = int.from_bytes(digest[:4], "big") & ((1 << 22) - 1)

        fake = TIMESTAMP_PREFIX | low_22
        return str(fake)
```

**Why structurally valid:** Discord's snowflake docs spec a 42-bit ms-since-epoch + 22 low bits. Any consumer that parses the timestamp out of a snowflake (Stoat doesn't, but other tooling might) won't get garbage. Length is consistently 18–19 digits, matching real Discord IDs.

**Why no sort-order preservation:** preserving sort order would require a counter/state, which breaks reproducibility (same real ID → same fake ID across runs). We accept the trade-off. Tests sort by `timestamp` (ISO 8601 strings, stable) instead of by ID.

**Salt:** committed as a hex constant in `scripts/capture-fixtures.py`. Reproducibility > secrecy (the salt protects against accidental cross-fixture-set ID collision, not against a determined attacker — there's nothing to attack). Salt also stored in shared 1Password as a backup.

### CDN/URL scrub

Discord exports embed URLs from many hosts. The scrub step rewrites every URL whose host is in the known-Discord-CDN set:

| Host | Where it appears | Scrub action |
|------|------------------|--------------|
| `cdn.discordapp.com` | Attachment URLs, emoji URLs (`/emojis/<id>.png`), avatar URLs (`/avatars/<userid>/<hash>.png`), sticker URLs | Replace host with `cdn.example.test`; extract numeric IDs from path segments and route through `IdMapper`; keep filename suffix |
| `media.discordapp.net` | Proxied attachments, embeds | Replace host with `media.example.test`; same path-ID rewriting |
| `images-ext-1.discordapp.net`, `images-ext-2.discordapp.net` | Proxied external images in embeds | Replace host with `images-ext.example.test`; opaque path (no IDs to rewrite) |
| `cdn.discord-attachments.com` | Newer attachment CDN variant | Replace host with `cdn-attachments.example.test`; same path-ID rewriting |
| `tenor.com` | GIF embeds | Replace host with `tenor.example.test`; opaque path |
| Any URL appearing in `embed.url`, `embed.image.url`, `embed.video.url`, `embed.thumbnail.url`, `embed.author.iconUrl`, `embed.footer.iconUrl` | User-supplied embed URLs (any host) | Replace host with `embed.example.test`; preserve path |

**URL-embedded IDs:** `cdn.discordapp.com/emojis/12345.png` and `cdn.discordapp.com/avatars/<userid>/<hash>.png` contain real snowflakes inside the path. The scrubber must:

1. Parse the URL.
2. For each path segment that matches the snowflake regex (`^\d{17,19}$`), replace it with `IdMapper.map_id(segment)`.
3. Reassemble. The resulting URL still embeds an ID, but the embedded ID is the fake one — so a test that checks `f"emojis/{message.reactions[0].emoji.id}" in attachment.url` continues to work after the scrub.

CI grep guard: any URL in `tests/fixtures/*.json` whose host is NOT in the allow-set (`cdn.example.test`, `media.example.test`, `images-ext.example.test`, `cdn-attachments.example.test`, `tenor.example.test`, `embed.example.test`, plus anything explicitly whitelisted) fails the build.

### Replaced fixtures

Per the issue's audit table:

| Fixture | Action |
|---------|--------|
| `dce_stdout_sample.txt` | REPLACED (captured DCE 2.47.1 stdout transcript) |
| `simple_channel.json` | REPLACED (captured single-channel real export) |
| `edge_cases.json` | REPLACED (captured channel with mixed media, edits, pins, stickers) |
| `markdown_rendered.json` | REPLACED (captured from a markdown-heavy channel) |
| `Test Server - general - Cool Thread [...].json` | REPLACED (captured real thread export) |
| `Test Server - Feedback Forum - Bug Report [...].json` | REPLACED (captured real forum-thread export) |
| `rollback_state.json` | KEEP (Ferry-internal state, not external) |

## Data flow

```
Peter runs:  uv run python scripts/capture-fixtures.py
    ↓
Loads FERRY_TEST_SERVER_ID + FERRY_TEST_DISCORD_BOT_TOKEN from .env
    ↓
asyncio.run(_run_capture(tmpdir)):
    download_dce() → run_dce_export(..., is_bot_token=True)
    ↓
DCE writes raw JSON files + media to tmp directory
    ↓
For each JSON file:
  IdMapper rewrites all snowflake IDs (incl. URL-embedded)
  Display names replaced with TestUserN
  CDN/embed URLs replaced (6+ hosts, see scrub table)
    ↓
Scrubbed files copied to tests/fixtures/, overwriting hand-authored ones
    ↓
DCE stdout transcript dumped to dce_stdout_sample.txt
    ↓
Peter inspects, runs `git diff tests/fixtures/`, commits if right.
```

## Components

| Component | Responsibility |
|-----------|----------------|
| `scripts/capture-fixtures.py` | Orchestrates capture + scrubbing; user-invoked, not run in CI |
| `scripts/_id_mapper.py` | `IdMapper` class + `scrub_export_dir()` helper (URL/name/ID scrubbing) |
| `scripts/_cdn_scrub.py` (or inline) | CDN URL host + embedded-ID rewriting |
| `tests/fixtures/README.md` | Documents how to regenerate, what test server should contain (per the 32-row table above), where to find the bot token + salt in 1Password |
| `.env.example` | Template showing required env vars (`FERRY_TEST_SERVER_ID`, `FERRY_TEST_DISCORD_BOT_TOKEN`) |
| `tests/fixtures/*.json` | Replaced contents (captured + scrubbed) |
| `tests/test_id_mapper.py` | Unit tests for `IdMapper` (snowflake validity, determinism, empty-string whitelist, raise on garbage) |
| `tests/test_*.py` (existing) | Updated to use structural assertions where they previously asserted on hand-crafted IDs (~300 lines of churn across `test_parser.py` + `test_transforms.py`) |
| `src/discord_ferry/exporter/runner.py` | Small additive refactor: `_build_dce_command` + `run_dce_export` accept optional `is_bot_token: bool = False`; `validate_discord_token` accepts `Bot ` prefix |

## Test brittleness mitigation

**Strategy: structural assertions, not value assertions.**

Today's tests assert things like `assert msg["author"]["id"] == "111"`. After this PR, those tests must assert structurally: `assert isinstance(msg["author"]["id"], str)` and (where useful) `assert msg["author"]["id"].isdigit()` and `17 <= len(msg["author"]["id"]) <= 19`.

Where exact values matter to prove behavior (e.g., "embed has 3 inline fields"), tests assert on the SHAPE of the data not the literal IDs:

```python
# Before:
assert message["author"]["name"] == "Peter"

# After:
assert message["author"]["name"].startswith("TestUser")
```

Where re-capturing might shift the order of returned fields (Discord doesn't guarantee order across API calls), tests use `set()` comparisons or `any()` predicates.

**Cross-message ID consistency:** because `IdMapper` is deterministic, tests can still assert "the user who posted message #1 also reacted to message #4" via `msg1.author.id == msg4.reactions[0].user_id` — same real ID maps to same fake ID, so the equality holds.

**Sort-order caveat:** tests must NOT rely on snowflake sort order (high bits are constant in fake IDs). Sort by `timestamp` field instead.

## Error handling

- Capture script fails fast if env vars missing (clear error message; exit code 2).
- Capture script fails fast if test server is empty / unreachable (DCE will emit non-zero exit; surface its stderr).
- `IdMapper.map_id` raises `ValueError` if asked to map non-snowflake input (catches scrub bugs); empty string is whitelisted.
- CI grep guard: any string in `tests/fixtures/*.json` that matches the snowflake regex but is NOT a fake-ID-range value (high bits != `TIMESTAMP_PREFIX >> 22`) fails the build.
- CI grep guard: any URL in `tests/fixtures/*.json` whose host is NOT in the allow-set fails the build.
- Tests using new fixtures fail loudly if structural shape changes (e.g., test expected 3 inline fields but only got 2 — test runner shows the actual fixture content).

## Phasing

**Three sub-PRs, sequenced.** The original "single PR" estimate underestimated the test-rewrite scope (~300 lines, not ~50). Splitting into three sub-PRs bounds risk per merge and lets sub-PR (b) — the manual capture step — be a tiny diff that's easy to review.

### Sub-PR (a): Capture infrastructure + IdMapper

**Scope:**
- New: `scripts/capture-fixtures.py`, `scripts/_id_mapper.py`, `scripts/_cdn_scrub.py`.
- New: `tests/test_id_mapper.py` — unit tests for `IdMapper` (snowflake validity, determinism, empty-string whitelist, raise on garbage, URL-embedded-ID rewriting).
- New: `tests/fixtures/README.md`, `.env.example`.
- Modified: `src/discord_ferry/exporter/runner.py` — additive `is_bot_token` param on `_build_dce_command` and `run_dce_export`; `validate_discord_token` accepts `Bot ` prefix.
- Modified: existing tests of `runner.py` to cover the new `is_bot_token=True` path.
- CI: add grep guards for non-fake snowflakes and non-allowlist URL hosts in `tests/fixtures/`.

**Does NOT touch:** existing fixtures (still hand-authored), existing parser/transforms tests.

**Risk:** low. Additive changes. Existing test suite continues to pass against hand-authored fixtures.

### Sub-PR (b): First capture (Peter, out-of-band)

**Scope:**
- Peter runs `uv run python scripts/capture-fixtures.py` against the seeded test server.
- Resulting scrubbed fixtures committed wholesale, replacing the hand-authored ones.
- `dce_stdout_sample.txt` regenerated from the same capture.

**Diff is data-only** (JSON + the stdout text file). No code changes.

**Risk:** medium. The existing tests will likely BREAK against the new fixtures (because they assert on hand-crafted IDs). This sub-PR is allowed to land with failing tests in `test_parser.py` / `test_transforms.py` — the failures are the work item for sub-PR (c). To keep CI green, mark the affected test files with a `pytest.mark.xfail(reason="rewrite pending in sub-PR c")` or a temporary skip in `conftest.py`. Document the temporary skip in the PR description with explicit "sub-PR (c) removes this skip" callout.

**Alternative:** land sub-PR (b) and sub-PR (c) as a single combined PR if the reviewer prefers no temporary-skip window. Decided during writing-plans based on review-bandwidth preference.

### Sub-PR (c): Rewrite test assertions (~300 lines)

**Scope:**
- Rewrite assertions in `tests/test_parser.py` (102 asserts; estimated ~120 lines of changes) — convert exact-value asserts on IDs/names to structural asserts.
- Rewrite assertions in `tests/test_transforms.py` (121 asserts; estimated ~150 lines of changes) — same pattern.
- Audit any other test file under `tests/` that loads fixtures (`grep -l "fixtures/" tests/`).
- Remove the temporary skip / xfail markers from sub-PR (b).
- Update `tests/fixtures/README.md` with the assertion-style guidance.

**Risk:** medium-low. Per-file mechanical work. Each rewritten test should still cover the same parser/transforms branch — verify by running coverage diff before/after.

**Sequencing:** lands AFTER #23's PRs (v2.1.4 + v2.2.0) AND #36 (isInline) — see issue #35 for sequencing rationale.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Hand-crafted fixtures are fine for unit tests" | FALSIFIED | At least one (`dce_stdout_sample.txt`) is provably wrong; high likelihood others are too |
| "Captured fixtures are too much work" | PARTIALLY FALSIFIED | One-time capture infra + small test server pays back on every DCE bump; but the test-rewrite is ~300 lines of churn (not 50). Worth it, but be honest about the cost. |
| "Schema is documented elsewhere so we don't need real fixtures" | FALSIFIED | DCE has no public schema doc; the JsonMessageWriter source is the only spec |
| "Deterministic ID mapping is the right scrubbing approach" | VERIFIED | Preserves cross-file ID relationships; reproducible across re-captures |
| "Test server should be dedicated, not shared" | VERIFIED | Avoids PII risk; gives us full control over content for edge-case coverage |
| "Bot token is the right capture credential (not user token)" | VERIFIED | User token = ToS violation (selfbotting); bot token + DCE `-b` flag is supported and correct |
| "HMAC-truncation IDs would be valid snowflakes" | FALSIFIED in original spec, FIXED in revision | Naive HMAC truncation produces garbage timestamps; revised IdMapper packs HMAC into low 22 bits with a fixed timestamp prefix |
| "CDN scrub of just `cdn.discordapp.com` is sufficient" | FALSIFIED in original spec, FIXED in revision | Real exports use 6+ hosts; user-supplied embed URLs add more; URL-embedded IDs need IdMapper rewriting |
| "Single-PR phasing is realistic" | FALSIFIED in original spec, FIXED in revision | ~300 lines of test churn justifies splitting into 3 sub-PRs |

**Foundational?** YES — fixtures are the contract definition for the parser layer.

## Risks

| Risk | Mitigation |
|------|------------|
| PII leak (real Discord IDs/avatars in committed fixtures) | IdMapper scrubs all IDs; CI grep guard for snowflakes outside the fake-ID range; CI grep guard for URLs outside the example.test allow-set |
| Fixture size inflation (real exports 10-100x larger than hand-crafted) | Test server is tiny by design (~32 messages); if files are still too big, compress with `git-lfs` (defer until measured) |
| Test brittleness (exact counts/IDs change on re-capture) | Structural assertions, not value assertions; documented in `tests/fixtures/README.md` |
| Test server availability (Peter's test server gets deleted, can't re-capture) | `tests/fixtures/README.md` documents server contents in detail (32-row table above) so it can be re-created; bot token + salt in shared 1Password |
| DCE behavior changes between captures (e.g., new fields added) | This is a feature, not a risk — we WANT to detect upstream change; tests fail and we update accordingly |
| Capture script becomes stale (Ferry changes invalidate the script) | Capture script imports from `discord_ferry` namespace, so any breaking change in Ferry's exporter API forces a script update |
| Bus factor on credentials (only Peter has bot token + salt) | Bot token + salt stored in shared 1Password vault entry (`stoat-ferry / fixture-capture/...`); README documents path |
| DCE version bump breaks fixtures (e.g., 2.47.1 → 2.48.0 changes JSON shape) | HARD-CUT versioning: replace fixtures wholesale per DCE bump; if a regression suite across DCE versions becomes necessary, revisit (deferred) |
| IdMapper empty-string footgun (`""` is a meaningful Discord sentinel for non-reply / uncategorized) | Whitelist `""` as pass-through in `map_id`; covered by `test_id_mapper.py` |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Automated weekly fixture re-capture in CI | Requires bot token + salt in CI secrets; substantial workflow work; bot would need to live in the test server long-term | (will file if needed) | After this issue closes |
| Property-based testing (Hypothesis) on top of captured fixtures | Higher value but bigger lift | (will file if interest emerges) | After this issue closes |
| Multiple fixtures per scenario (small/medium/large) | YAGNI; one per scenario is enough until we have a reason | — | If a specific test needs a different size |
| Git-lfs for fixture storage | Premature; measure repo size first | — | If `du -sh tests/fixtures/` exceeds ~5 MB after capture |
| Side-by-side DCE-version fixtures (`dce-2.47.1/` + `dce-2.48.0/`) | YAGNI; hard-cut is simpler. A regression test across versions is hypothetical demand. | — | If a real regression bug requires comparing parser behavior across DCE versions |
| Sort-order-preserving IdMapper | Requires counter/state, breaks reproducibility. Tests can sort by `timestamp` instead. | — | If a test genuinely needs ID-order semantics (none today) |
| Capturing `dce_stderr_sample.txt` (stderr transcript) | Cheap to add but no consumer yet; can be added when stderr-handling tests need it | — | When #23's stderr handling needs fixture coverage |

## Open questions (for implementation, not blocking spec)

- RecipientAdd system-message coverage: this message type only appears in group DMs (not guild channels), and our test "server" is a guild. Options for writing-plans: (a) hand-craft a single fixture for this one branch with a comment explaining why; (b) add a separate group-DM-flavored capture mode; (c) accept the gap and `xfail` the relevant test branch. (a) seems cheapest.
- `transcript_path` mechanism for capturing DCE stdout: easiest is to add a `transcript_path: Path | None = None` kwarg to `run_dce_export` that tee's stdout to the file. Or: re-run with a stream wrapper. Pick during writing-plans.
- Whether sub-PR (b) and sub-PR (c) merge separately (with a temporary skip window) or as one combined PR. Defer to reviewer-bandwidth preference.
- Which URLs to whitelist as "user-supplied" vs "scrub" — `embed.url` is user-supplied, but `embed.image.url` may be Discord-proxied. Conservative default: scrub all embed URLs uniformly. Loosen if a test breaks.

## Cross-references

- Issue #23 — provides `parse_dce_line` (which the captured `dce_stdout_sample.txt` will be replayed through via #34's Design C)
- Issue #34 — Design C (fixture replay) consumes the new captured `dce_stdout_sample.txt`; Design A is independent
- Issue #36 — when this fixture replacement lands, the captured fixture will have real `isInline` keys, validating #36's parser fix end-to-end
- Critique pass `2026-05-15-critique-pass.md` — section "### #35" enumerates the 6 critical findings + 3 minor gaps that this revision addresses
