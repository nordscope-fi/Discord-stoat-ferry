"""Tests for Discord → Stoat permission bit translation."""

from discord_ferry.discord.permissions import (
    ALL_STOAT_PERMISSIONS,
    DISCORD_TO_STOAT,
    STOAT_PERMISSION_BITS,
    translate_permissions,
)


def test_zero_permissions() -> None:
    assert translate_permissions(0) == 0


def test_single_bit_manage_channels() -> None:
    # Discord MANAGE_CHANNELS = bit 4 → Stoat ManageChannel = bit 0
    assert translate_permissions(1 << 4) == 1 << 0


def test_single_bit_send_messages() -> None:
    # Discord SEND_MESSAGES = bit 11 → Stoat SendMessage = bit 22
    assert translate_permissions(1 << 11) == 1 << 22


def test_manage_roles_maps_to_three_bits() -> None:
    # Discord MANAGE_ROLES = bit 28 → Stoat ManagePermissions (2) + ManageRole (3).
    # Batch 5 (#109) added AssignRoles (9), which Stoat splits out of ManageRole.
    result = translate_permissions(1 << 28)
    assert result & (1 << 2)  # ManagePermissions set
    assert result & (1 << 3)  # ManageRole set
    assert result & (1 << 9)  # AssignRoles set
    assert result == (1 << 2) | (1 << 3) | (1 << 9)


def test_administrator_expands_to_all() -> None:
    # Discord ADMINISTRATOR = bit 3 → ALL_STOAT_PERMISSIONS
    assert translate_permissions(1 << 3) == ALL_STOAT_PERMISSIONS


def test_administrator_with_other_bits() -> None:
    # ADMINISTRATOR dominates — result is ALL regardless of other bits
    discord_bits = (1 << 3) | (1 << 11)
    assert translate_permissions(discord_bits) == ALL_STOAT_PERMISSIONS


def test_unmapped_bits_dropped() -> None:
    # Discord USE_EXTERNAL_EMOJIS = bit 12, no Stoat equivalent → dropped
    # (KICK_MEMBERS bit 1 is now mapped as of Batch 9; use a still-unmapped bit.)
    assert translate_permissions(1 << 12) == 0


def test_multiple_mapped_bits() -> None:
    # SEND_MESSAGES (11) + ATTACH_FILES (15)
    discord_bits = (1 << 11) | (1 << 15)
    expected = (1 << 22) | (1 << 27)  # SendMessage + UploadFiles
    assert translate_permissions(discord_bits) == expected


def test_all_mapped_bits() -> None:
    # Set every mapped Discord bit and verify all Stoat bits are set
    discord_bits = 0
    for bit in DISCORD_TO_STOAT:
        discord_bits |= 1 << bit
    result = translate_permissions(discord_bits)
    assert result & (1 << 0)  # ManageChannel
    assert result & (1 << 1)  # ManageServer
    assert result & (1 << 22)  # SendMessage
    assert result & (1 << 29)  # React
    # Batch 5 (#109): bits above 29 are now reachable without ADMINISTRATOR.
    assert result & (1 << 30)  # Connect
    assert result & (1 << 36)  # Listen
    assert result & (1 << 37)  # MentionEveryone
    assert result & (1 << 40)  # ViewAuditLogs


def test_all_stoat_permissions_value() -> None:
    """Pin ALL_STOAT_PERMISSIONS against an independent transcription.

    The constant is derived from STOAT_PERMISSION_BITS, so asserting it against
    that same set would be circular. This list is transcribed straight from
    stoatchat's `ChannelPermission` enum in declaration order, and the decimal
    literal pins the resulting value.
    """
    expected = (
        (1 << 0)  # ManageChannel
        | (1 << 1)  # ManageServer
        | (1 << 2)  # ManagePermissions
        | (1 << 3)  # ManageRole
        | (1 << 4)  # ManageCustomisation
        | (1 << 6)  # KickMembers
        | (1 << 7)  # BanMembers
        | (1 << 8)  # TimeoutMembers
        | (1 << 9)  # AssignRoles
        | (1 << 10)  # ChangeNickname
        | (1 << 11)  # ManageNicknames
        | (1 << 12)  # ChangeAvatar
        | (1 << 13)  # RemoveAvatars
        | (1 << 20)  # ViewChannel
        | (1 << 21)  # ReadMessageHistory
        | (1 << 22)  # SendMessage
        | (1 << 23)  # ManageMessages
        | (1 << 24)  # ManageWebhooks
        | (1 << 25)  # InviteOthers
        | (1 << 26)  # SendEmbeds
        | (1 << 27)  # UploadFiles
        | (1 << 28)  # Masquerade
        | (1 << 29)  # React
        | (1 << 30)  # Connect
        | (1 << 31)  # Speak
        | (1 << 32)  # Video
        | (1 << 33)  # MuteMembers
        | (1 << 34)  # DeafenMembers
        | (1 << 35)  # MoveMembers
        | (1 << 36)  # Listen
        | (1 << 37)  # MentionEveryone
        | (1 << 38)  # MentionRoles
        | (1 << 39)  # BypassSlowmode
        | (1 << 40)  # ViewAuditLogs
    )
    assert expected == ALL_STOAT_PERMISSIONS
    assert ALL_STOAT_PERMISSIONS == 2_199_022_223_327


def test_administrator_deny_returns_zero() -> None:
    """ADMINISTRATOR in deny context must NOT expand to all bits."""
    assert translate_permissions(1 << 3, is_deny=True) == 0


def test_administrator_allow_still_expands() -> None:
    """ADMINISTRATOR in allow context preserves existing behavior."""
    assert translate_permissions(1 << 3, is_deny=False) == ALL_STOAT_PERMISSIONS
    assert translate_permissions(1 << 3) == ALL_STOAT_PERMISSIONS


def test_deny_view_channel_translates() -> None:
    """Normal deny bits translate correctly through the mapping."""
    result = translate_permissions(1 << 10, is_deny=True)
    assert result == 1 << 20


def test_deny_multiple_bits() -> None:
    """Deny with multiple mapped bits translates each one."""
    discord_bits = (1 << 10) | (1 << 11)
    result = translate_permissions(discord_bits, is_deny=True)
    expected = (1 << 20) | (1 << 22)
    assert result == expected


def test_deny_unmapped_bits_dropped() -> None:
    """Unmapped Discord bits in deny context are silently dropped."""
    # bit 12 (USE_EXTERNAL_EMOJIS) has no Stoat equivalent (bit 1 is now mapped).
    assert translate_permissions(1 << 12, is_deny=True) == 0


def test_deny_administrator_with_other_bits_preserves_others() -> None:
    """C1 fix: ADMINISTRATOR + VIEW_CHANNEL in deny strips ADMIN, keeps VIEW_CHANNEL."""
    discord_bits = (1 << 3) | (1 << 10)  # ADMINISTRATOR + VIEW_CHANNEL
    result = translate_permissions(discord_bits, is_deny=True)
    assert result == 1 << 20  # Only ViewChannel deny, ADMIN stripped


# ---------------------------------------------------------------------------
# Batch 9 — S5 perm-map fidelity (7 newly-mapped Discord permissions)
# ---------------------------------------------------------------------------


def test_new_permission_mappings() -> None:
    """SC-29: each newly-added Discord→Stoat permission maps correctly."""
    pairs = [
        (0, 25),  # CREATE_INSTANT_INVITE → InviteOthers
        (1, 6),  # KICK_MEMBERS → KickMembers
        (2, 7),  # BAN_MEMBERS → BanMembers
        (26, 10),  # CHANGE_NICKNAME → ChangeNickname
        (27, 11),  # MANAGE_NICKNAMES → ManageNicknames
        (29, 24),  # MANAGE_WEBHOOKS → ManageWebhooks
    ]
    for discord_bit, stoat_bit in pairs:
        assert translate_permissions(1 << discord_bit) == 1 << stoat_bit


# NOTE: `test_mention_everyone_dropped` lived here. It asserted
# `translate_permissions(1 << 17) == 0` on the strength of a code comment
# claiming Stoat has no MentionEveryone. The comment was false (bit 37, with
# MentionRoles at 38), so the test was pinning a bug. Superseded by
# `test_mention_everyone_maps_to_both_stoat_bits` below.


def test_admin_grants_new_bits() -> None:
    """SC-30: the ADMIN short-circuit now includes the 7 new bits."""
    result = translate_permissions(1 << 3)
    assert result == ALL_STOAT_PERMISSIONS
    for bit in (6, 7, 10, 11, 24, 25):
        assert result & (1 << bit)


# ---------------------------------------------------------------------------
# Batch 5 (#109) — permission parity with Stoat's full 34-bit enum
# ---------------------------------------------------------------------------


def test_voice_permissions_map() -> None:
    """SC-24: the Discord voice bits reach their Stoat counterparts.

    Before this batch every one of these translated to 0, which is why migrated
    voice channels granted nobody the right to speak or moderate a call.
    """
    assert translate_permissions(1 << 21) == 1 << 31  # SPEAK → Speak
    assert translate_permissions(1 << 22) == 1 << 33  # MUTE_MEMBERS → MuteMembers
    assert translate_permissions(1 << 23) == 1 << 34  # DEAFEN_MEMBERS → DeafenMembers
    assert translate_permissions(1 << 24) == 1 << 35  # MOVE_MEMBERS → MoveMembers
    assert translate_permissions(1 << 9) == 1 << 32  # STREAM → Video


def test_connect_also_grants_listen() -> None:
    """Discord CONNECT means join AND hear; Stoat splits those in two.

    ``voice_join.rs:52`` gates joining on Connect, but ``voice_client.rs:95``
    sets the LiveKit token's ``can_subscribe`` from Listen. Granting Connect
    alone puts the user in the room with no audio or video from anyone.
    """
    assert translate_permissions(1 << 20) == (1 << 30) | (1 << 36)


def test_mention_everyone_maps_to_both_stoat_bits() -> None:
    """Discord MENTION_EVERYONE covers @everyone, @here AND role pings.

    Supersedes ``test_mention_everyone_dropped``: the comment claiming Stoat has
    no MentionEveryone was false — it is bit 37, with MentionRoles at 38.
    """
    assert translate_permissions(1 << 17) == (1 << 37) | (1 << 38)


def test_moderate_members_maps_to_timeout() -> None:
    assert translate_permissions(1 << 40) == 1 << 8  # MODERATE_MEMBERS → TimeoutMembers


def test_view_audit_log_maps() -> None:
    assert translate_permissions(1 << 7) == 1 << 40  # VIEW_AUDIT_LOG → ViewAuditLogs


def test_manage_roles_also_grants_assign_roles() -> None:
    """MANAGE_ROLES covers assigning roles to members, which Stoat splits out."""
    assert translate_permissions(1 << 28) == (1 << 2) | (1 << 3) | (1 << 9)


def test_bypass_slowmode_maps() -> None:
    """Discord's explicit BYPASS_SLOWMODE (52) → Stoat BypassSlowmode (39).

    Ferry migrates slowmode, so a moderator exempt at source would otherwise be
    throttled at destination. Deliberately NOT inferred from MANAGE_MESSAGES:
    Discord ships a dedicated bit for this, so no derivation is needed.
    """
    assert translate_permissions(1 << 52) == 1 << 39
    # ...and the permissions it is sometimes conflated with stay single-target.
    assert translate_permissions(1 << 13) == 1 << 23  # MANAGE_MESSAGES
    assert translate_permissions(1 << 4) == 1 << 0  # MANAGE_CHANNELS


def test_admin_expansion_covers_every_defined_bit_and_nothing_else() -> None:
    """SC-25: all 34 defined bits, and nothing in Stoat's 41-52 free area."""
    assert len(STOAT_PERMISSION_BITS) == 34
    for bit in STOAT_PERMISSION_BITS:
        assert ALL_STOAT_PERMISSIONS & (1 << bit), f"bit {bit} missing from admin expansion"
    assert ALL_STOAT_PERMISSIONS < (1 << 41)
    # Explicitly NOT GrantAllSafe: Stoat computes that for owners without ever
    # persisting it to a role, and it spans the undefined free area.
    assert ALL_STOAT_PERMISSIONS != 0x000F_FFFF_FFFF_FFFF


def test_admin_expansion_stays_inside_stoat_s_own_numeric_bounds() -> None:
    """The value crossed 2**32 in batch 5 (#109) — pin what it must still fit.

    Stoat stores permissions as i64, and its enum carries the comment "This should
    be restricted to the lower 52 bits to prevent any potential issues with
    Javascript". Both bounds are asserted so a future bit addition that breaks
    either one fails here rather than at a client.
    """
    assert ALL_STOAT_PERMISSIONS < 2**63 - 1  # i64
    assert ALL_STOAT_PERMISSIONS < 2**53  # exactly representable as a JS number


def test_every_mapped_target_is_a_defined_stoat_bit() -> None:
    """A typo'd target would otherwise grant a bit Stoat does not define."""
    for target in DISCORD_TO_STOAT.values():
        bits = target if isinstance(target, list) else [target]
        for bit in bits:
            assert bit in STOAT_PERMISSION_BITS, f"target bit {bit} is not a Stoat permission"


def test_admin_grants_voice_and_mention_bits() -> None:
    """The old hand-rolled OR-chain omitted every bit above 29."""
    result = translate_permissions(1 << 3)
    for bit in (8, 9, 12, 13, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40):
        assert result & (1 << bit), f"admin expansion missing bit {bit}"
