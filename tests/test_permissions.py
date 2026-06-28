"""Tests for Discord → Stoat permission bit translation."""

from discord_ferry.discord.permissions import (
    ALL_STOAT_PERMISSIONS,
    DISCORD_TO_STOAT,
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


def test_manage_roles_maps_to_two_bits() -> None:
    # Discord MANAGE_ROLES = bit 28 → Stoat ManagePermissions (2) + ManageRole (3)
    result = translate_permissions(1 << 28)
    assert result & (1 << 2)  # ManagePermissions set
    assert result & (1 << 3)  # ManageRole set
    assert result == (1 << 2) | (1 << 3)


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


def test_all_stoat_permissions_value() -> None:
    # Verify ALL_STOAT_PERMISSIONS matches the documented sum (incl. the 6
    # Batch 9 additions: bits 6,7,10,11,24,25).
    expected = (
        1
        | 2
        | 4
        | 8
        | 16
        | 64
        | 128
        | 1_024
        | 2_048
        | 1_048_576
        | 2_097_152
        | 4_194_304
        | 8_388_608
        | 16_777_216
        | 33_554_432
        | 67_108_864
        | 134_217_728
        | 268_435_456
        | 536_870_912
    )
    assert expected == ALL_STOAT_PERMISSIONS


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


def test_mention_everyone_dropped() -> None:
    """MENTION_EVERYONE (bit 17) has no Stoat equivalent → dropped."""
    assert translate_permissions(1 << 17) == 0


def test_admin_grants_new_bits() -> None:
    """SC-30: the ADMIN short-circuit now includes the 7 new bits."""
    result = translate_permissions(1 << 3)
    assert result == ALL_STOAT_PERMISSIONS
    for bit in (6, 7, 10, 11, 24, 25):
        assert result & (1 << bit)
