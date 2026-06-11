"""Permission overwrite helpers."""
import discord

ADMIN_ROLE_NAMES = ["ORG", "CAP", "TABBY", "EQUITY"]
MEMBER_ROLE_NAMES = ["INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR"]
JUDGE_ROLE_NAMES = ["INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR"]
ALL_ROLE_NAMES = ADMIN_ROLE_NAMES + MEMBER_ROLE_NAMES
TRACKED_ROLE_NAMES = ALL_ROLE_NAMES  # 8 mutually exclusive roles


def everyone_deny(guild: discord.Guild) -> dict:
    return {guild.default_role: discord.PermissionOverwrite(view_channel=False)}


def _ow_full() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=True,
        manage_messages=True, add_reactions=True, attach_files=True,
        embed_links=True, connect=True, speak=True, stream=True,
        use_voice_activation=True, move_members=True,
    )


def _ow_send() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=True,
        add_reactions=True, attach_files=True, embed_links=True,
        connect=True, speak=True, use_voice_activation=True,
    )


def _ow_read_only() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=False,
        add_reactions=True, connect=True, speak=False, use_voice_activation=True,
    )


def _ow_view_only() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=False,
        add_reactions=False,
    )


def allow_full(roles_dict: dict, *names: str) -> dict:
    out = {}
    for n in names:
        r = roles_dict.get(n)
        if r:
            out[r] = _ow_full()
    return out


def allow_send(roles_dict: dict, *names: str) -> dict:
    out = {}
    for n in names:
        r = roles_dict.get(n)
        if r:
            out[r] = _ow_send()
    return out


def allow_read_only(roles_dict: dict, *names: str) -> dict:
    out = {}
    for n in names:
        r = roles_dict.get(n)
        if r:
            out[r] = _ow_read_only()
    return out


def allow_view_only(roles_dict: dict, *names: str) -> dict:
    out = {}
    for n in names:
        r = roles_dict.get(n)
        if r:
            out[r] = _ow_view_only()
    return out


def merge(*dicts: dict) -> dict:
    out = {}
    for d in dicts:
        out.update(d)
    return out
