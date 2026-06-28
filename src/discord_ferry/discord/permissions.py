"""Discord → Stoat permission bit translation."""

# Discord permission bit position → Stoat permission bit position(s).
# Discord bits not in this map have no Stoat equivalent and are dropped.
DISCORD_TO_STOAT: dict[int, int | list[int]] = {
    0: 25,  # CREATE_INSTANT_INVITE → InviteOthers
    1: 6,  # KICK_MEMBERS → KickMembers
    2: 7,  # BAN_MEMBERS → BanMembers
    4: 0,  # MANAGE_CHANNELS → ManageChannel
    5: 1,  # MANAGE_GUILD → ManageServer
    28: [2, 3],  # MANAGE_ROLES → ManagePermissions + ManageRole
    30: 4,  # MANAGE_EMOJIS → ManageCustomisation
    10: 20,  # VIEW_CHANNEL → ViewChannel
    16: 21,  # READ_MESSAGE_HISTORY → ReadMessageHistory
    11: 22,  # SEND_MESSAGES → SendMessage
    13: 23,  # MANAGE_MESSAGES → ManageMessages
    14: 26,  # EMBED_LINKS → SendEmbeds
    15: 27,  # ATTACH_FILES → UploadFiles
    6: 29,  # ADD_REACTIONS → React
    # NOTE: MENTION_EVERYONE (Discord bit 17) has NO Stoat equivalent — the Stoat
    # Permission enum has no MentionEveryone — so it is intentionally dropped.
    26: 10,  # CHANGE_NICKNAME → ChangeNickname
    27: 11,  # MANAGE_NICKNAMES → ManageNicknames
    29: 24,  # MANAGE_WEBHOOKS → ManageWebhooks
}

ALL_STOAT_PERMISSIONS = (
    1
    | 2
    | 4
    | 8
    | 16  # bits 0-4
    | 64
    | 128  # bits 6-7 (KickMembers, BanMembers)
    | 1_024
    | 2_048  # bits 10-11 (ChangeNickname, ManageNicknames)
    | 1_048_576
    | 2_097_152  # bits 20-21
    | 4_194_304
    | 8_388_608  # bits 22-23
    | 16_777_216
    | 33_554_432  # bits 24-25 (ManageWebhooks, InviteOthers)
    | 67_108_864
    | 134_217_728  # bits 26-27
    | 268_435_456
    | 536_870_912  # bits 28-29
)


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
