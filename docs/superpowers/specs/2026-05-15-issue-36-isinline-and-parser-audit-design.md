# Design: isInline parser fix (issue #36)

**Date:** 2026-05-15 (revised 2026-05-16)
**Issue:** [#36](https://github.com/nordscope-fi/discord-stoat-ferry/issues/36)
**Ships as:** Single PR
**Status:** Spec — awaiting implementation

## Revisions from critique pass (2026-05-16)

The critique pass identified that this spec was over-engineered for a problem the codebase actually has. Resolutions:

| Critique finding | Resolution in this revision |
|------------------|---------------------------|
| Spot-check shows likely n=1-3 fixes, not 50 | **Audit performed inline; actual finding count: 1. Re-scoped accordingly.** |
| Audit script regex would produce false positives | **Dropped audit script; manual audit was sufficient** |
| DCE source has Newtonsoft `[JsonProperty]` attributes the regex would miss | Manual audit reads source directly (DCE 2.47.1 actually uses `Utf8JsonWriter` with literal `_writer.WriteX("key", ...)` calls — Newtonsoft is not in the writer path; the spot-check regex would have worked, but the audit was small enough to do by hand anyway) |
| Two-PR structure for ≤3 fixes is over-engineering | **Collapsed to single PR** |
| Sequencing "after #23" is wrong | **Corrected: independent of #23** |
| Decision Accountability gate self-verifies | Updated entries to reflect critique findings |

## Problem

`src/discord_ferry/parser/transforms.py:210` reads `field_obj.get("inline", False)`, but DCE 2.47.1 serializes embed field inline as `isInline` (verified at [`JsonMessageWriter.cs:259`](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.47.1/DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs#L259) — `_writer.WriteBoolean("isInline", embedField.IsInline)`). Every multi-field embed in every real export currently renders without inline grouping (the inline row collapses to a non-inline block list).

Hand-authored fixtures in `tests/fixtures/*.json` and `tests/test_transforms.py` all use the wrong key `"inline"` too, so existing tests pass green against the wrong-key assumption. This makes the bug invisible to the test suite.

## Audit (performed during this revision)

The brainstorming hypothesis — "the parser is riddled with similar typos" — is **falsified by inline audit**.

**Method:** read DCE 2.47.1 [`JsonMessageWriter.cs`](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.47.1/DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs) (648 lines) and extract every `_writer.WriteX("key", ...)` call. Cross-reference against every `obj.get("...")` and `obj["..."]` in `src/discord_ferry/parser/transforms.py` and `src/discord_ferry/parser/dce_parser.py`.

**Result:** 1 typo. Only `isInline`.

### Audit appendix (full cross-reference)

**Ferry keys read in `transforms.py` `flatten_embed`:**

| Ferry key (file:line) | DCE write call | Verdict |
|---|---|---|
| `embed.get("author")` (188) | `WritePropertyName("author")` (288) | MATCH |
| `author.get("name")` (190) | `WriteString("name", embedAuthor.Name)` (147) | MATCH |
| `embed.get("description")` (195) | `WriteString("description", ...)` (279) | MATCH |
| `embed.get("fields")` (200) | `WriteStartArray("fields")` (325) | MATCH |
| `field_obj.get("name", "")` (206) | `WriteString("name", ...)` (254) | MATCH |
| `field_obj.get("value", "")` (207) | `WriteString("value", ...)` (256) | MATCH |
| **`field_obj.get("inline", False)` (210)** | **`WriteBoolean("isInline", ...)` (259)** | **TYPO — fix in this PR** |
| `embed.get("footer")` (226) | `WritePropertyName("footer")` (312) | MATCH |
| `footer.get("text")` (228) | `WriteString("text", embedFooter.Text)` (228) | MATCH |
| `embed.get("title")` (235) | `WriteString("title", ...)` (273) | MATCH |
| `embed.get("url")` (239) | `WriteString("url", embed.Url)` (276) | MATCH |
| `embed.get("color")` (244) | `WriteString("color", ...)` (284) | MATCH |
| `author.get("iconUrl")` (250) | `WriteString("iconUrl", ...)` (153) | MATCH |
| `embed.get("thumbnail")` / `image` (262, 274) | `WritePropertyName("thumbnail" / "image")` (294, 300) | MATCH |
| `media_obj.get("url", "")` (264, 276) | `WriteString("url", ...)` (177) | MATCH |

**Ferry keys read in `transforms.py` `flatten_poll`:** `poll.question`, `poll.answers`, `text`, `votes`, etc. **DCE 2.47.1 does not export polls at all** (no `poll` writes anywhere in `JsonMessageWriter.cs`; `gh search code "poll repo:Tyrrrz/DiscordChatExporter"` returns only one HTML reply test mentioning `"used /poll"`). Ferry's `_parse_message` reads `raw.get("poll")` defensively and gets `None` for every real DCE message, so `flatten_poll` is never invoked from the migrator path. Not a typo; not a Category B; functionally dead code w.r.t. DCE input. Out of scope for this PR. Tracked as a future-feature item if DCE ever adds poll export.

**Ferry keys read in `transforms.py` `handle_stickers`:** `sticker.name`, `sticker.sourceUrl` — both MATCH (DCE writes both at lines 364, 367).

**Ferry keys read in `dce_parser.py`:**

Top-level: `guild`, `channel`, `messages`, `messageCount`, `exportedAt` — all MATCH (DCE writes at lines 382, 394, 428, 636, 425).

`_parse_guild`: `id`, `name`, `iconUrl` — all MATCH (lines 383, 384, 387).

`_parse_channel`: `id`, `type`, `name`, `categoryId`, `category`, `topic` — all MATCH (lines 395, 396, 402, 399, 400, 403).

`_parse_message`: `id`, `type`, `timestamp`, `content`, `isPinned`, `timestampEdited`, `author`, `attachments`, `embeds`, `stickers`, `reactions`, `mentions`, `reference`, `poll` — all MATCH except `poll` (DCE doesn't write it; Ferry's `.get("poll")` returns `None` defensively — fine).

`_parse_reference`: `messageId`, `channelId`, `guildId` — all MATCH (lines 543, 544, 545).

`_parse_author`: `id`, `name`, `discriminator`, `nickname`, `color`, `isBot`, `avatarUrl`, `roles` — all MATCH (lines 49–63).

`_parse_role`: `id`, `name`, `color`, `position` — all MATCH (lines 110–113).

`_parse_attachment`: `id`, `url`, `fileName`, `fileSizeBytes` — all MATCH (lines 129, 131, 134, 135).

`_parse_reaction`: `emoji`, `count`, plus `emoji.{id, name, isAnimated, imageUrl}` — all MATCH (lines 504, 507, 86–92).

Other `qs.get("ex")` in `check_cdn_url_expiry` is parsing Discord CDN URL query strings, not DCE JSON keys — not in scope for the audit (the critique correctly flagged this would be a false positive of the dropped script).

**DCE keys Ferry doesn't read (not typos, but visible gaps; out of scope):**
- `code` on emoji (DCE 89) — Ferry uses `name` only.
- `iconCanonicalUrl` on author/footer (DCE 160, 240) — fallback CDN URL Ferry doesn't surface.
- `canonicalUrl` on image/video (DCE 184, 211).
- `width`, `height` on image/video (DCE 187, 188, 214, 215).
- `timestamp` on embed (DCE 277).
- `images` array on embed (DCE 317–322) — separate from `image` singular.
- `inlineEmojis` array on message and embed (DCE 333, 609) — pre-extracted custom emoji.
- `dateRange` (DCE 419), `callEndedTimestamp` (DCE 451), `forwardedMessage` (DCE 552), `interaction` (DCE 597) — Ferry doesn't surface these in v2.x.

These are feature gaps, not bugs. File separate issues if any becomes valuable.

## Architecture

Single-PR change:

1. **Fix `transforms.py:210`** — change `field_obj.get("inline", False)` to `field_obj.get("isInline", False)`.
2. **Update test fixtures** that use the wrong key — `tests/fixtures/*.json` files containing `"inline":` in embed field positions. (Note: most fixtures don't have inline embed fields; only test snippets in `tests/test_transforms.py` use the literal key.)
3. **Update existing `tests/test_transforms.py` tests** that pass dict literals with `"inline"`: change to `"isInline"` so they continue to validate the (now-correctly-keyed) parser. Tests at lines 310-312, 325, 338-342, 361-366, 422-423.
4. **Add one new regression test** asserting that an embed dict with the DCE-correct key `"isInline"` (and *not* `"inline"`) produces inline grouping. This test is what would have caught the bug originally.

## Components

| Component | Responsibility |
|-----------|----------------|
| `src/discord_ferry/parser/transforms.py` (FIXED) | One key change at line 210: `"inline"` → `"isInline"` |
| `tests/test_transforms.py` (UPDATED) | Existing inline-related tests updated to use `"isInline"`; one new regression test |
| `CHANGELOG.md` (UPDATED) | `### Fixed` entry under upcoming version, noting user-visible impact (every multi-field embed previously rendered without inline grouping) |

## Testing

Existing tests (lines 306-374, 422-423 in `tests/test_transforms.py`) assert inline grouping behavior using the wrong key — they pass today only because Ferry's parser also uses the wrong key. After the fix, these tests will fail unless updated to `"isInline"`. The update is mechanical.

**New regression test** in `tests/test_transforms.py`:

```python
def test_embed_field_inline_grouping_uses_dce_key() -> None:
    """Regression for #36: parser must read DCE's `isInline`, not `inline`.

    Before the fix, the parser read `inline` and silently ignored the real
    `isInline` key, so every multi-field embed rendered as a stacked list
    instead of a pipe-separated row.
    """
    # DCE 2.47.1 writes `isInline` (JsonMessageWriter.cs:259).
    embed: dict[str, object] = {
        "fields": [
            {"name": "A", "value": "1", "isInline": True},
            {"name": "B", "value": "2", "isInline": True},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    # If the parser reads the wrong key, both fields collapse to non-inline blocks
    # (`**A**\n1\n\n**B**\n2`), and `**A** | **B**` is absent.
    assert "**A** | **B**" in desc
    assert "1 | 2" in desc
```

## Phasing

Single PR. CHANGELOG entry under the next-shipping version (likely v2.1.4 or whatever the current `[Unreleased]` block becomes):

```markdown
### Fixed
- Embed fields with `isInline: true` now correctly render as pipe-separated rows
  in migrated messages. The parser previously read the wrong key (`inline`),
  causing every multi-field Discord embed to render as a stacked list since v1.
  Re-running migration on a server with bot-posted embeds will produce visibly
  cleaner output. (#36)
```

### Sequencing relative to other issues

- **Independent of #23** — #23 introduces `dce_output.py` (a new module for parsing DCE stdout lines). It does not touch `transforms.py` or `dce_parser.py`. This PR can ship in parallel or first.
- **Lands BEFORE #35** — #35 captures real DCE output as fixtures. Landing this fix first means #35's captured fixtures will validate the fix end-to-end (the captured DCE output will contain `isInline: true` for any embed-using test message, and the parser will correctly group them).

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Just fix isInline" (one-line fix) | **VERIFIED via inline audit (2026-05-16)** | The full cross-reference of every Ferry-side `obj.get("...")` against every DCE `_writer.WriteX("...")` finds exactly one mismatch. The brainstorming hypothesis "this is one instance of a pattern" was falsified by data. |
| "Two PRs (audit + fixes) is right separation" | **FALSIFIED by critique** | Audit finds n=1; two-PR scaffolding adds review burden without adding signal. Single PR is honest. |
| "Audit script catches more than manual reading" | **FALSIFIED by critique** | DCE 2.47.1 `JsonMessageWriter.cs` is 648 lines with all writes done literally as `_writer.WriteX("key", ...)`. Manual reading is exhaustive in <30 minutes. The script's regex would have produced false positives (e.g., `qs.get("ex")` for CDN URL parsing) without yielding additional findings. |
| "Lands AFTER #23" | **FALSIFIED by critique** | #23 doesn't modify `transforms.py` or `dce_parser.py`. No conflict. Independent. |

**Foundational?** NO — this is a single-key bugfix with one regression test. The audit appendix is documentation; it doesn't impose contracts on future work beyond "if you bump DCE, re-skim `JsonMessageWriter.cs` for new writes."

## Risks

| Risk | Mitigation |
|------|------------|
| Existing test fixtures or production code elsewhere relies on `"inline"` (the wrong key) | grep confirms no production references outside `transforms.py:210`; tests using literal `"inline"` are updated mechanically as part of this PR |
| Future DCE bump renames `isInline` again | Re-skim `JsonMessageWriter.cs` on every DCE bump (existing process per `DCE_VERSION` checklist); #34's contract test is the longer-term backstop |
| Fix introduces visual regressions in already-migrated embeds | Migration is one-shot per server; previously migrated content is not re-rendered. Only future migrations benefit from the fix. |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Surface DCE keys Ferry doesn't read (`width`, `height`, `images` array, `inlineEmojis`, `forwardedMessage`, `interaction`) | Feature work, not bugfix | (file per-field if interest emerges) | When a specific gap is judged valuable |
| Automated audit on every DCE bump | Coupled to #34's contract-test maturity; #34 is in spec phase | (will file later) | After #34 lands and proves itself |
| Poll handling against real DCE output | DCE 2.47.1 doesn't export polls; `flatten_poll` is dead code w.r.t. DCE input | (will file if DCE adds poll export) | When DCE adds `WriteX("poll", ...)` |

## Cross-references

- Issue #23 — orthogonal; introduces `dce_output.py` for stdout parsing. Doesn't touch the modules this PR fixes.
- Issue #34 — Design A (DCE `--help` test) is the longer-term contract that would catch DCE-side renames; out of scope here.
- Issue #35 — captured-real fixtures will validate this fix end-to-end once they land. Sequencing: this PR before #35.
