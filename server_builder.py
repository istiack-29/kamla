import discord
import asyncio
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES, config_manager
)

ROLE_CONFIG = [
    {"name": "ORG",                    "color": discord.Color.red(),        "administrator": True,  "hoist": True},
    {"name": "CAP",                    "color": discord.Color.orange(),     "administrator": True,  "hoist": True},
    {"name": "TABBY",                  "color": discord.Color.yellow(),     "administrator": True,  "hoist": True},
    {"name": "EQUITY",                 "color": discord.Color.green(),      "administrator": True,  "hoist": True},
    {"name": "INVITED ADJUDICATOR",    "color": discord.Color.blue(),       "administrator": False, "hoist": True},
    {"name": "INDEPENDENT ADJUDICATOR","color": discord.Color.purple(),     "administrator": False, "hoist": True},
    {"name": "DEBATER",                "color": discord.Color.teal(),       "administrator": False, "hoist": True},
    {"name": "VISITOR",                "color": discord.Color.light_grey(), "administrator": False, "hoist": False},
]


def _everyone_deny(*perms) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    for p in perms:
        setattr(ow, p, False)
    return ow


def _allow(*perms) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    for p in perms:
        setattr(ow, p, True)
    return ow


def _role_ow(admin: bool = False, view: bool = True, send: bool = True) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite(view_channel=view)
    if view:
        ow.read_message_history = True
    if send:
        ow.send_messages = True
        ow.attach_files = True
        ow.embed_links = True
    if admin:
        ow.manage_messages = True
    return ow


async def wipe_server(guild: discord.Guild) -> None:
    for channel in list(guild.channels):
        if "kamla-config" in channel.name:
            continue
        try:
            await channel.delete(reason="KAMLA server wipe")
            await asyncio.sleep(0.4)
        except Exception:
            pass

    bot_role = guild.me.top_role
    protected = {guild.default_role.id, bot_role.id}

    for role in list(guild.roles):
        if role.id in protected or role.name in KAMLA_ROLE_NAMES:
            continue
        try:
            await role.delete(reason="KAMLA server wipe")
            await asyncio.sleep(0.3)
        except Exception:
            pass


async def _create_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    roles: dict[str, discord.Role] = {}

    existing = {r.name: r for r in guild.roles}
    for cfg in reversed(ROLE_CONFIG):
        name = cfg["name"]
        if name in existing:
            roles[name] = existing[name]
            continue
        try:
            perms = discord.Permissions()
            if cfg["administrator"]:
                perms = discord.Permissions(administrator=True)
            else:
                perms = discord.Permissions(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    connect=True,
                    speak=True,
                    stream=True,
                    use_voice_activation=True,
                    embed_links=True,
                    attach_files=True,
                    add_reactions=True,
                )
            role = await guild.create_role(
                name=name,
                color=cfg["color"],
                hoist=cfg["hoist"],
                permissions=perms,
                reason="KAMLA tournament setup",
            )
            roles[name] = role
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"[Builder] Failed to create role {name}: {e}")

    return roles


def _build_admin_overwrites(guild, roles, bot_ow=None):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: bot_ow or _allow("view_channel", "send_messages", "read_message_history",
                                   "manage_messages", "embed_links", "attach_files"),
    }
    for rn in ADMIN_ROLE_NAMES:
        if rn in roles:
            ow[roles[rn]] = _role_ow(admin=True, view=True, send=True)
    return ow


def _build_all_role_overwrites(guild, roles, send=True):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _allow("view_channel", "send_messages", "read_message_history",
                         "manage_messages", "embed_links", "attach_files"),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            ow[roles[rn]] = _role_ow(
                admin=rn in ADMIN_ROLE_NAMES,
                view=True,
                send=send or rn in ADMIN_ROLE_NAMES,
            )
    return ow


def _build_announce_overwrites(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _allow("view_channel", "send_messages", "read_message_history",
                         "manage_messages", "embed_links", "attach_files"),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            is_admin = rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=is_admin,
                manage_messages=is_admin,
                embed_links=is_admin,
                attach_files=is_admin,
            )
    return ow


def _build_public_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        ),
        guild.me: _allow("view_channel", "send_messages", "read_message_history",
                         "manage_messages", "embed_links", "attach_files"),
    }


def _room_text_ow(guild, roles):
    return _build_all_role_overwrites(guild, roles, send=True)


async def build_server(guild: discord.Guild, fmt: str, rooms: int, timezone: str,
                       tournament_name: str, creator_id: int) -> None:
    roles = await _create_roles(guild)

    # ── Public channels (top, no category) ──────────────────────────────────
    pub_ow = _build_public_overwrites(guild)

    meet_ch = await guild.create_text_channel("👐🏻︱meet-the-developer", overwrites=pub_ow)
    await guild.create_text_channel("👋🏻︱welcome", overwrites=pub_ow)
    await guild.create_text_channel("❓︱get-role", overwrites=pub_ow)
    await guild.create_text_channel("🦧︱how-to-use-this-server", overwrites=pub_ow)

    # ── ORGCOM ───────────────────────────────────────────────────────────────
    orgcom_ow = _build_admin_overwrites(guild, roles)
    orgcom_cat = await guild.create_category("⚜️︱ORGCOM", overwrites=orgcom_ow)
    settings_ch = await guild.create_text_channel("⚙️︱settings",  category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_text_channel("🏴︱org",      category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_text_channel("📂︱document", category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_voice_channel("ORG",          category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_voice_channel("Control Room", category=orgcom_cat, overwrites=orgcom_ow)

    # ── ASSIGN ───────────────────────────────────────────────────────────────
    assign_ow = _build_admin_overwrites(guild, roles)
    assign_cat = await guild.create_category("🛅︱ASSIGN", overwrites=assign_ow)
    for rn in ["ORG", "CAP", "TABBY", "EQUITY", "DEBATER", "VISITOR",
               "INDEPENDENT ADJUDICATOR", "INVITED ADJUDICATOR"]:
        await guild.create_text_channel(rn.lower().replace(" ", "-"),
                                        category=assign_cat, overwrites=assign_ow)
        await asyncio.sleep(0.2)

    # ── GRAND AUDITORIUM ─────────────────────────────────────────────────────
    ga_all_ow = _build_all_role_overwrites(guild, roles, send=True)
    ga_ann_ow = _build_announce_overwrites(guild, roles)
    ga_cat = await guild.create_category("🏟️︱GRAND AUDITORIUM",
                                         overwrites=_build_all_role_overwrites(guild, roles))
    await guild.create_text_channel("📨︱ga-text",      category=ga_cat, overwrites=ga_all_ow)
    await guild.create_text_channel("🎓︱motion",       category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱announcement", category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱break",        category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱matchup",      category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱ballot",       category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_voice_channel("🏟️︱GRAND AUDITORIUM",
                                     category=ga_cat,
                                     overwrites=_build_all_role_overwrites(guild, roles))

    # ── INFORMATION ──────────────────────────────────────────────────────────
    info_ow_cat = _build_all_role_overwrites(guild, roles, send=False)
    info_send_ow = _build_announce_overwrites(guild, roles)
    info_cat = await guild.create_category("ℹ️︱INFORMATION", overwrites=info_ow_cat)
    for ch_name in ["schedule", "important-forms", "debater-briefing",
                    "judge-briefing", "equity-briefing"]:
        await guild.create_text_channel(ch_name, category=info_cat, overwrites=info_send_ow)
        await asyncio.sleep(0.2)

    # ── ROOMS ────────────────────────────────────────────────────────────────
    await _create_rooms(guild, roles, fmt, rooms)

    # ── Populate static channels ─────────────────────────────────────────────
    await _post_meet_developer(meet_ch)
    await _post_welcome(guild)
    await _post_get_role(guild)
    await _post_how_to_use(guild)
    await _post_settings_panel(settings_ch, guild, fmt, rooms, timezone,
                               tournament_name, creator_id)

    import config as cfg_mod
    cfg_data = {
        "format": fmt,
        "rooms": rooms,
        "timezone": timezone,
        "tournament_name": tournament_name,
        "created_by": creator_id,
        "created_at": discord.utils.utcnow().isoformat(),
        "settings_channel_id": settings_ch.id,
    }
    await config_manager.set_config(guild, cfg_data)


async def _create_rooms(guild: discord.Guild, roles: dict, fmt: str, count: int) -> None:
    r_text_ow = _build_all_role_overwrites(guild, roles, send=True)

    adj_roles = ["ORG", "CAP", "TABBY", "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR"]
    all_in_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _allow("view_channel", "send_messages", "read_message_history",
                         "manage_messages", "embed_links", "attach_files"),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            is_adj = rn in adj_roles
            all_in_ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=is_adj,
            )

    voice_ow = _build_all_role_overwrites(guild, roles)

    for i in range(1, count + 1):
        room_name = f"ROOM {i:02d}"
        cat = await guild.create_category(room_name, overwrites=voice_ow)
        await guild.create_text_channel("timer",  category=cat, overwrites=r_text_ow)
        await guild.create_text_channel("poi",    category=cat, overwrites=r_text_ow)
        all_in_ch = await guild.create_text_channel("all-in", category=cat, overwrites=all_in_ow)
        debate_vc = await guild.create_voice_channel("DEBATE ROOM",    category=cat, overwrites=voice_ow)
        await guild.create_voice_channel("ADJUDICATION", category=cat, overwrites=voice_ow)

        if fmt.lower() == "ap":
            await guild.create_voice_channel("GOVT PREP", category=cat,
                                             overwrites=voice_ow, user_limit=3)
            await guild.create_voice_channel("OPP PREP",  category=cat,
                                             overwrites=voice_ow, user_limit=3)
        else:
            for prep in ["OG PREP", "OO PREP", "CG PREP", "CO PREP"]:
                await guild.create_voice_channel(prep, category=cat,
                                                 overwrites=voice_ow, user_limit=2)
                await asyncio.sleep(0.2)

        from allin_cog import AllInView
        await all_in_ch.send(
            embed=discord.Embed(
                title="🔴 Push Back All",
                description="Press the button to move all members from prep rooms back to **DEBATE ROOM**.",
                color=discord.Color.red(),
            ),
            view=AllInView(),
        )
        await asyncio.sleep(0.4)


async def _post_meet_developer(channel: discord.TextChannel) -> None:
    embed = discord.Embed(
        title="👐 Meet the Developer",
        description=(
            "KAMLA was built by **Istiack** — a passionate developer and debater.\n\n"
            "Use the buttons below to get in touch, support the project, or create a tournament."
        ),
        color=discord.Color.blurple(),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Developer Contact", url="https://istiack.pages.dev/#contact", style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Donate", url="https://istiack.pages.dev/#donate", style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Create Tournament", url="https://kamla-bot.pages.dev", style=discord.ButtonStyle.link))

    import os, pathlib
    logo_path = pathlib.Path(__file__).parent / "m.png"
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            file = discord.File(f, filename="m.png")
            embed.set_image(url="attachment://m.png")
            await channel.send(embed=embed, file=file, view=view)
    else:
        await channel.send(embed=embed, view=view)


async def _post_welcome(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="👋🏻︱welcome")
    if not channel:
        return
    embed = discord.Embed(
        title="👋 Welcome to the Tournament Server!",
        description=(
            "This server is powered by **KAMLA** — an automated tournament management bot.\n\n"
            "**How to get a role:**\n"
            "1. Head to **❓︱get-role**\n"
            "2. Click the button that matches your role in this tournament\n"
            "3. Your role will be assigned instantly\n\n"
            "**Role descriptions:**\n"
            "🔵 **Invited Adjudicator** — Officially invited judges\n"
            "🟣 **Independent Adjudicator** — Self-registered judges\n"
            "🩵 **Debater** — Competing speakers\n"
            "⬜ **Visitor** — Observers and guests\n\n"
            "If you need an admin role (ORG/CAP/TABBY/EQUITY), contact the tournament organisers."
        ),
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed)


async def _post_get_role(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="❓︱get-role")
    if not channel:
        return

    from roles_cog import RoleSelectView
    embed = discord.Embed(
        title="❓ Get Your Role",
        description=(
            "Click a button below to claim your role.\n\n"
            "You may only hold **one** role at a time. Clicking a new button will remove your previous role.\n\n"
            "⚠️ If you are ORG / CAP / TABBY / EQUITY, your role is assigned by the organisers — "
            "do **not** click here; contact the tournament ORG instead."
        ),
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed, view=RoleSelectView())


async def _post_how_to_use(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="🦧︱how-to-use-this-server")
    if not channel:
        return
    embed = discord.Embed(
        title="🦧 How to Use This Server",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📌 Channels Overview",
        value=(
            "• **welcome** — Read this first\n"
            "• **get-role** — Claim your tournament role\n"
            "• **GRAND AUDITORIUM** — Main hub for all participants\n"
            "• **INFORMATION** — Official documents and briefings\n"
            "• **ROOM XX** — Individual debate rooms"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ Timer System",
        value=(
            "In any **#timer** channel, send a time like:\n"
            "`10m` | `1h` | `15s` | `1h10m` | `10m30s`\n"
            "The bot will start a live countdown."
        ),
        inline=False,
    )
    embed.add_field(
        name="🙋 POI System",
        value=(
            "In any **#poi** channel, send `poi`, `POI`, or `Poi`.\n"
            "The bot logs your POI request with a timestamp."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔴 All-In Button",
        value=(
            "In **#all-in**, press **Push Back All** to move everyone from prep rooms to the Debate Room.\n"
            "Only adjudicators and admins may use this."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎭 Roles",
        value=(
            "• **ORG / CAP / TABBY / EQUITY** — Admin roles (assigned by organisers)\n"
            "• **Invited Adjudicator** — Official judges\n"
            "• **Independent Adjudicator** — Self-registered judges\n"
            "• **Debater** — Competitors\n"
            "• **Visitor** — Observers"
        ),
        inline=False,
    )
    await channel.send(embed=embed)


async def _post_settings_panel(channel: discord.TextChannel, guild: discord.Guild,
                                fmt: str, rooms: int, timezone: str,
                                tournament_name: str, creator_id: int) -> None:
    from settings_cog import build_settings_embed, SettingsView
    embed = build_settings_embed(
        tournament_name=tournament_name,
        fmt=fmt,
        rooms=rooms,
        timezone=timezone,
        creator_id=creator_id,
        created_at=discord.utils.utcnow().isoformat(),
    )
    msg = await channel.send(embed=embed, view=SettingsView())
    return msg
