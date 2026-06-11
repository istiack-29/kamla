"""Builder — nuke, roles, categories, channels, rooms."""
import asyncio
import discord
from permissions import (
    ADMIN_ROLE_NAMES, MEMBER_ROLE_NAMES, JUDGE_ROLE_NAMES, ALL_ROLE_NAMES,
    everyone_deny, allow_full, allow_send, allow_read_only, allow_view_only, merge,
)

ROLE_SPECS = [
    ("ORG", 0xFF0000, True, True),
    ("CAP", 0xFF8800, True, True),
    ("TABBY", 0xFFFF00, True, True),
    ("EQUITY", 0x00FF88, True, True),
    ("INVITED ADJUDICATOR", 0x00AAFF, False, True),
    ("INDEPENDENT ADJUDICATOR", 0x8800FF, False, True),
    ("DEBATER", 0xFF00AA, False, True),
    ("VISITOR", 0x888888, False, False),
]


async def _safe(coro):
    try:
        return await coro
    except discord.HTTPException as e:
        if e.status == 429:
            await asyncio.sleep(getattr(e, "retry_after", 2) or 2)
            try:
                return await coro
            except discord.HTTPException:
                return None
        return None


async def nuke_channels(guild: discord.Guild):
    for ch in list(guild.channels):
        try:
            await ch.delete()
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.05)


async def nuke_roles(guild: discord.Guild):
    me_top = guild.me.top_role
    for r in list(guild.roles):
        if r.is_default() or r.managed:
            continue
        if r >= me_top:
            continue
        try:
            await r.delete()
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.05)


async def create_all_roles(guild: discord.Guild) -> dict:
    roles = {}
    # Create from bottom upwards so ORG ends up at top
    for name, color, admin, hoist in reversed(ROLE_SPECS):
        perms = discord.Permissions(administrator=True) if admin else discord.Permissions.none()
        try:
            r = await guild.create_role(
                name=name, colour=discord.Colour(color),
                permissions=perms, hoist=hoist, mentionable=True,
            )
            roles[name] = r
            await asyncio.sleep(0.1)
        except discord.HTTPException:
            pass
    return roles


# ---------- Standalone channels ----------
async def create_standalone_channels(guild: discord.Guild, roles: dict, wizard_data: dict):
    from reaction_roles import GetRoleView, MeetDevView

    deny = everyone_deny(guild)
    me_ow = {guild.me: discord.PermissionOverwrite(
        view_channel=True, send_messages=True, manage_messages=True,
        add_reactions=True, embed_links=True, attach_files=True,
    )}

    view_all = {guild.default_role: discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=False,
        add_reactions=True,
    )}

    # A) meet-the-developer
    ch = await guild.create_text_channel("👐🏻︱meet-the-developer", overwrites=merge(view_all, me_ow))
    embed = discord.Embed(
        title="Meet the Developer — istiack",
        description="KAMLA was built with ❤️ by **istiack**. Visit the links below.",
        color=0xFF69B4,
    )
    await ch.send(embed=embed, view=MeetDevView())
    try:
        await ch.send(file=discord.File("m.png"))
    except (FileNotFoundError, discord.HTTPException):
        pass

    # B) welcome
    ch = await guild.create_text_channel("👋🏻︱welcome", overwrites=merge(view_all, me_ow))
    tname = wizard_data.get("tournament_name", "the Tournament")
    embed = discord.Embed(
        title=f"Welcome to {tname}! 🎉",
        description=(
            "We're delighted to have you here.\n\n"
            "📌 **New members:** Head to <#❓︱get-role> to claim your role.\n"
            "🔒 **ORG / CAP / TABBY / EQUITY:** Do NOT self-assign — contact @ORG for manual role assignment."
        ),
        color=0x00FFAA,
    )
    if wizard_data.get("tournament_logo_url"):
        embed.set_thumbnail(url=wizard_data["tournament_logo_url"])
    await ch.send(embed=embed)

    # C) get-role
    get_role_ow = {guild.default_role: discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=False, add_reactions=True,
    )}
    ch = await guild.create_text_channel("❓︱get-role", overwrites=merge(get_role_ow, me_ow))
    embed = discord.Embed(
        title="🎭 Claim Your Role",
        description=(
            "Click a button below to claim your role. You can only hold one role at a time.\n\n"
            "🧑‍⚖️ **Invited Adjudicator**\n"
            "👨‍⚖️ **Independent Adjudicator**\n"
            "🗣️ **Debater**\n"
            "👀 **Visitor**\n\n"
            "⚠️ ORG / CAP / TABBY / EQUITY are assigned manually by staff."
        ),
        color=0x5865F2,
    )
    await ch.send(embed=embed, view=GetRoleView())

    # D) how-to-use-this-server
    ch = await guild.create_text_channel("🦧︱how-to-use-this-server", overwrites=merge(view_all, me_ow))
    embed = discord.Embed(
        title="🦧 How to Use This Server",
        description=(
            "**1. Get Your Role** — Visit <#❓︱get-role> and click a button.\n\n"
            "**2. The 8 Roles**\n"
            "• `ORG` `CAP` `TABBY` `EQUITY` — Admin staff, full access\n"
            "• `INVITED / INDEPENDENT ADJUDICATOR` — Judges, can view Adjudication rooms\n"
            "• `DEBATER` — Compete in debate rooms\n"
            "• `VISITOR` — Watch only\n\n"
            "**3. Debate Rooms** — Each room has Debate, Prep, and Adjudication voice channels.\n\n"
            "**4. Timer** — In `#timer`, type a duration like `1h30m`, `45s`, or `2h15m30s`. "
            "The bot starts a countdown. Anything else gets deleted.\n\n"
            "**5. POI** — Type `poi` in `#poi` to raise a Point of Information.\n\n"
            "**6. Role Assignment** — ORG / CAP / TABBY / EQUITY are assigned in the 🛅 Assign channels.\n\n"
            "**7. All-In** — In `#all-in`, admins/judges can push everyone in prep rooms back to the Debate Room."
        ),
        color=0xFFAA00,
    )
    await ch.send(embed=embed)


# ---------- ORGCOM ----------
async def create_orgcom_category(guild: discord.Guild, roles: dict):
    base = merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES))
    cat = await guild.create_category("⚜️︱ORGCOM", overwrites=base)

    await guild.create_text_channel("⚙️︱settings", category=cat,
        overwrites=merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES)))
    await guild.create_text_channel("🏴︱org", category=cat,
        overwrites=merge(everyone_deny(guild), allow_full(roles, "ORG")))
    await guild.create_text_channel("📂︱document", category=cat,
        overwrites=merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES)))
    await guild.create_voice_channel("ORG", category=cat,
        overwrites=merge(everyone_deny(guild), allow_full(roles, "ORG")))
    await guild.create_voice_channel("Control Room", category=cat,
        overwrites=merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES)))
    return cat


# ---------- Assign ----------
ASSIGN_CHANNELS = ["CAP", "ORG", "TABBY", "DEBATER", "EQUITY", "VISITOR",
                   "INDEPENDENT ADJUDICATOR", "INVITED ADJUDICATOR"]


async def create_assign_category(guild: discord.Guild, roles: dict):
    base = merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES))
    cat = await guild.create_category("🛅︱ASSIGN", overwrites=base)
    for role_name in ASSIGN_CHANNELS:
        ch = await guild.create_text_channel(
            role_name.lower().replace(" ", "-"), category=cat,
            overwrites=merge(everyone_deny(guild), allow_full(roles, *ADMIN_ROLE_NAMES)),
        )
        embed = discord.Embed(
            title=f"Role Assignment — {role_name}",
            description=(
                f"Mention a user here to assign them the **{role_name}** role.\n"
                f"Example: `@username`\n\n"
                f"The bot will automatically assign/remove roles. "
                f"Deleting a user's mention removes their role."
            ),
            color=0x3498DB,
        )
        msg = await ch.send(embed=embed)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
    return cat


# ---------- Grand Auditorium ----------
async def create_grand_auditorium(guild: discord.Guild, roles: dict):
    base = merge(everyone_deny(guild), allow_view_only(roles, *ALL_ROLE_NAMES))
    cat = await guild.create_category("🏟️︱GRAND AUDITORIUM", overwrites=base)

    # ga-text: everyone in ALL_ROLES can send
    await guild.create_text_channel("📨︱ga-text", category=cat,
        overwrites=merge(everyone_deny(guild), allow_send(roles, *ALL_ROLE_NAMES)))

    for name in ["🎓︱motion", "🎓︱announcement", "🎓︱break", "🎓︱matchup"]:
        ow = merge(everyone_deny(guild),
                   allow_view_only(roles, *MEMBER_ROLE_NAMES),
                   allow_send(roles, *ADMIN_ROLE_NAMES))
        await guild.create_text_channel(name, category=cat, overwrites=ow)

    # ballot: ORG + TABBY + CAP send; others view
    ow = merge(everyone_deny(guild),
               allow_view_only(roles, *ALL_ROLE_NAMES),
               allow_send(roles, "ORG", "TABBY", "CAP"))
    await guild.create_text_channel("🎓︱ballot", category=cat, overwrites=ow)

    # voice
    await guild.create_voice_channel("🏟️︱GRAND AUDITORIUM", category=cat,
        overwrites=merge(everyone_deny(guild), allow_send(roles, *ALL_ROLE_NAMES)))
    return cat


# ---------- Information ----------
async def create_information_category(guild: discord.Guild, roles: dict):
    base = merge(everyone_deny(guild),
                 allow_view_only(roles, *MEMBER_ROLE_NAMES),
                 allow_send(roles, *ADMIN_ROLE_NAMES))
    cat = await guild.create_category("ℹ️︱INFORMATION", overwrites=base)
    for name in ["schedule", "important-forms", "debater-briefing", "judge-briefing", "equity-briefing"]:
        await guild.create_text_channel(name, category=cat)
    return cat


# ---------- Rooms ----------
async def create_room(guild: discord.Guild, roles: dict, room_num: int, fmt: str):
    from reaction_roles import AllInView

    base = merge(everyone_deny(guild), allow_view_only(roles, *ALL_ROLE_NAMES))
    cat = await guild.create_category(f"Room {room_num}", overwrites=base)

    # timer
    timer_ow = merge(everyone_deny(guild),
                     allow_send(roles, *ADMIN_ROLE_NAMES, "INVITED ADJUDICATOR",
                                "INDEPENDENT ADJUDICATOR", "DEBATER"),
                     allow_view_only(roles, "VISITOR"))
    await guild.create_text_channel("timer", category=cat, overwrites=timer_ow)

    # poi
    poi_ow = merge(everyone_deny(guild),
                   allow_send(roles, *ADMIN_ROLE_NAMES, "INVITED ADJUDICATOR",
                              "INDEPENDENT ADJUDICATOR", "DEBATER"),
                   allow_view_only(roles, "VISITOR"))
    await guild.create_text_channel("poi", category=cat, overwrites=poi_ow)

    # all-in
    allin_ow = merge(everyone_deny(guild),
                     allow_send(roles, *ADMIN_ROLE_NAMES, *JUDGE_ROLE_NAMES))
    allin = await guild.create_text_channel("all-in", category=cat, overwrites=allin_ow)
    embed = discord.Embed(
        title="🔴 PUSH BACK ALL",
        description="Click to forcefully move all members from prep rooms to the Debate Room.",
        color=0xFF0000,
    )
    await allin.send(embed=embed, view=AllInView())

    # debate room voice
    await guild.create_voice_channel("DEBATE ROOM", category=cat,
        overwrites=merge(everyone_deny(guild), allow_send(roles, *ALL_ROLE_NAMES)))

    prep_ow = merge(everyone_deny(guild),
                    allow_send(roles, *ADMIN_ROLE_NAMES, *JUDGE_ROLE_NAMES, "DEBATER"))

    if fmt == "AP":
        await guild.create_voice_channel("GOVT PREP", category=cat, overwrites=prep_ow, user_limit=3)
        await guild.create_voice_channel("OPP PREP", category=cat, overwrites=prep_ow, user_limit=3)
    else:  # BP
        await guild.create_voice_channel("OG PREP", category=cat, overwrites=prep_ow, user_limit=2)
        await guild.create_voice_channel("OO PREP", category=cat, overwrites=prep_ow, user_limit=2)
        await guild.create_voice_channel("CG PREP", category=cat, overwrites=prep_ow, user_limit=2)
        await guild.create_voice_channel("CO PREP", category=cat, overwrites=prep_ow, user_limit=2)

    adj_ow = merge(everyone_deny(guild), allow_send(roles, *JUDGE_ROLE_NAMES, "CAP"))
    await guild.create_voice_channel("ADJUDICATION", category=cat, overwrites=adj_ow)
    return cat


async def create_all_rooms(guild: discord.Guild, roles: dict, wizard_data: dict):
    n = wizard_data.get("room_count", 0)
    fmt = wizard_data.get("format", "AP")
    for i in range(1, n + 1):
        await create_room(guild, roles, i, fmt)
        await asyncio.sleep(0.2)
