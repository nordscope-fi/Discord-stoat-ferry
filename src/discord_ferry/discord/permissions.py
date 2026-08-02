"""Discord → Stoat permission bit translation."""

# Discord permission bit position → Stoat permission bit position(s).
# Discord bits not in this map have no Stoat equivalent and are dropped.
#
# Discord bit numbers verified against discordjs/discord-api-types
# `payloads/common.ts`; Stoat targets against STOAT_PERMISSION_BITS below.
DISCORD_TO_STOAT: dict[int, int | list[int]] = {
    0: 25,  # CREATE_INSTANT_INVITE → InviteOthers
    1: 6,  # KICK_MEMBERS → KickMembers
    2: 7,  # BAN_MEMBERS → BanMembers
    4: 0,  # MANAGE_CHANNELS → ManageChannel
    5: 1,  # MANAGE_GUILD → ManageServer
    7: 40,  # VIEW_AUDIT_LOG → ViewAuditLogs
    28: [2, 3, 9],  # MANAGE_ROLES → ManagePermissions + ManageRole + AssignRoles
    30: 4,  # MANAGE_EMOJIS → ManageCustomisation
    10: 20,  # VIEW_CHANNEL → ViewChannel
    16: 21,  # READ_MESSAGE_HISTORY → ReadMessageHistory
    11: 22,  # SEND_MESSAGES → SendMessage
    13: 23,  # MANAGE_MESSAGES → ManageMessages
    14: 26,  # EMBED_LINKS → SendEmbeds
    15: 27,  # ATTACH_FILES → UploadFiles
    6: 29,  # ADD_REACTIONS → React
    17: [37, 38],  # MENTION_EVERYONE → MentionEveryone + MentionRoles
    26: 10,  # CHANGE_NICKNAME → ChangeNickname
    27: 11,  # MANAGE_NICKNAMES → ManageNicknames
    29: 24,  # MANAGE_WEBHOOKS → ManageWebhooks
    40: 8,  # MODERATE_MEMBERS → TimeoutMembers
    52: 39,  # BYPASS_SLOWMODE → BypassSlowmode
    # Voice.
    9: 32,  # STREAM → Video
    20: [30, 36],  # CONNECT → Connect + Listen (see below)
    21: 31,  # SPEAK → Speak
    22: 33,  # MUTE_MEMBERS → MuteMembers
    23: 34,  # DEAFEN_MEMBERS → DeafenMembers
    24: 35,  # MOVE_MEMBERS → MoveMembers
}

# --- Notes on the non-obvious entries ----------------------------------------
#
# CONNECT → Connect + Listen is the one mapping NOT justified by a name match.
# Stoat gates *joining* a voice channel on Connect (`voice_join.rs:52`) but builds
# the LiveKit token's `can_subscribe` from Listen (`voice_client.rs:95`). Discord's
# CONNECT confers the ability to hear, so granting Connect alone drops the user
# into a room where nobody is audible. Stoat's own DEFAULT_PERMISSION bundles
# Connect/Speak/Listen/Video together, which corroborates the intent.
#
# MENTION_EVERYONE → MentionEveryone + MentionRoles is a name match on both
# targets: Discord's single bit covers @everyone, @here and role pings alike.
#
# Deliberately NOT mapped:
#
# * Discord PIN_MESSAGES (51). Stoat has no pin-only bit -- pinning lives under
#   ManageMessages (23), which also permits deletion. Routing 51 there would hand
#   deletion rights to someone who only had permission to pin.
# * Discord CREATE_GUILD_EXPRESSIONS (43), for the same reason against
#   ManageCustomisation (4).
# * Everything Stoat has no analogue for: threads, events, polls, soundboard,
#   stickers, application commands, insights, TTS, priority speaker, VAD.
#
# Stoat bits with no Discord analogue, reachable only through the ADMINISTRATOR
# expansion: 12 ChangeAvatar, 13 RemoveAvatars, 28 Masquerade.
#
# AssignRoles (9) is granted at CHANNEL-override scope too, as a side effect of
# MANAGE_ROLES appearing in a Discord channel overwrite. That is inert rather
# than an over-grant: Stoat checks it against `calculate_server_permissions`
# (`member_edit.rs:55,89`), which does not apply channel overrides.

# Every bit position defined by Stoat's `ChannelPermission` enum, mirrored from
# stoatchat/stoatchat `crates/core/permissions/src/models/channel.rs` (commit
# 502203d3, read 2026-08-02). Read the SOURCE, not the developer docs site --
# the docs lagged source, which is how a correct MentionEveryone mapping came to
# be deleted as "nonexistent" in v2.6.15.
#
# Bit 5 and bits 14-19 are undefined gaps. The enum reserves bits 41-52 as a
# declared "free area" and marks 53-64 do-not-use, so this set must never be
# widened speculatively.
STOAT_PERMISSION_BITS: frozenset[int] = frozenset(
    {
        0,  # ManageChannel
        1,  # ManageServer
        2,  # ManagePermissions
        3,  # ManageRole
        4,  # ManageCustomisation
        6,  # KickMembers
        7,  # BanMembers
        8,  # TimeoutMembers
        9,  # AssignRoles
        10,  # ChangeNickname
        11,  # ManageNicknames
        12,  # ChangeAvatar
        13,  # RemoveAvatars
        20,  # ViewChannel
        21,  # ReadMessageHistory
        22,  # SendMessage
        23,  # ManageMessages
        24,  # ManageWebhooks
        25,  # InviteOthers
        26,  # SendEmbeds
        27,  # UploadFiles
        28,  # Masquerade
        29,  # React
        30,  # Connect
        31,  # Speak
        32,  # Video
        33,  # MuteMembers
        34,  # DeafenMembers
        35,  # MoveMembers
        36,  # Listen
        37,  # MentionEveryone
        38,  # MentionRoles
        39,  # BypassSlowmode
        40,  # ViewAuditLogs
    }
)


def _bitfield(bits: frozenset[int]) -> int:
    """OR together ``1 << bit`` for every bit position in ``bits``."""
    value = 0
    for bit in bits:
        value |= 1 << bit
    return value


# The expansion applied when a Discord role holds ADMINISTRATOR. Derived from the
# enum above rather than hand-maintained -- the previous hand-rolled OR-chain
# covered only the bits that happened to be mapped, so migrated admins silently
# lost every voice, mention and audit-log permission.
#
# Deliberately NOT Stoat's `GrantAllSafe` (0x000F_FFFF_FFFF_FFFF): that spans
# bits 0-51, including the undefined free area, and Stoat computes it for server
# owners without ever persisting it to a role.
ALL_STOAT_PERMISSIONS = _bitfield(STOAT_PERMISSION_BITS)


def translate_permissions(discord_bits: int, *, is_deny: bool = False) -> int:
    """Convert a Discord permission bitfield to a Stoat permission bitfield.

    If ADMINISTRATOR (bit 3) is set in allow context, returns ALL_STOAT_PERMISSIONS.
    In deny context, ADMINISTRATOR is skipped (denying ADMIN in Discord doesn't
    mean "deny all" in Stoat). Unmapped Discord bits are silently dropped.
    """
    if discord_bits & (1 << 3):  # ADMINISTRATOR
        if is_deny:
            # Strip ADMIN bit, translate remaining deny bits normally.
            # Denying ADMIN in Discord doesn't mean "deny all" in Stoat,
            # but other deny bits alongside ADMIN still carry real meaning.
            discord_bits &= ~(1 << 3)
            if discord_bits == 0:
                return 0
            # Fall through to normal translation below
        else:
            return ALL_STOAT_PERMISSIONS

    stoat_bits = 0
    for discord_bit, stoat_target in DISCORD_TO_STOAT.items():
        if discord_bits & (1 << discord_bit):
            if isinstance(stoat_target, list):
                for bit in stoat_target:
                    stoat_bits |= 1 << bit
            else:
                stoat_bits |= 1 << stoat_target
    return stoat_bits
