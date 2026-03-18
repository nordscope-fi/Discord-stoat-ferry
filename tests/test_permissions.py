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
    # Discord KICK_MEMBERS = bit 1, no Stoat equivalent → dropped
    assert translate_permissions(1 << 1) == 0


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
    # Verify ALL_STOAT_PERMISSIONS matches the documented sum
    expected = (
        1
        | 2
        | 4
        | 8
        | 16
        | 1_048_576
        | 2_097_152
        | 4_194_304
        | 8_388_608
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
    assert translate_permissions(1 << 1, is_deny=True) == 0
