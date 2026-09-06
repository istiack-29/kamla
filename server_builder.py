import discord
import asyncio
import re
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES, config_manager
)

_building_guilds: set[int] = set()
_restoring_guilds: set[int] = set()

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

ORG_ACCESS_ROLES = ["ORG", "CAP", "TABBY", "EQUITY", "INVITED ADJUDICATOR"]

# ── Protected structure ────────────────────────────────────────────────────────

PROTECTED_CATEGORIES = {
    "⚜️︱ORGCOM",
    "🛅︱ASSIGN",
    "🏟️︱GRAND AUDITORIUM",
    "🎮︱PLAY",
    "ℹ️︱INFORMATION",
}

PROTECTED_TOP_LEVEL_CHANNELS = {
    "👐🏻︱meet-the-developer",
    "👋🏻︱welcome",
    "❓︱get-role",
    "🦧︱how-to-use-this-server",
}

CATEGORY_CHANNEL_STRUCTURE: dict[str, dict] = {
    "⚜️︱ORGCOM": {
        "text":  ["⚙️︱settings", "🏴︱org", "📂︱document"],
        "voice": ["ORG", "Control Room"],
    },
    "🛅︱ASSIGN": {
        "text":  ["org", "cap", "tabby", "equity", "debater", "visitor",
                  "independent-adjudicator", "invited-adjudicator"],
        "voice": [],
    },
    "🏟️︱GRAND AUDITORIUM": {
        "text":  ["📨︱ga-text", "🎓︱motion", "🎓︱announcement", "🎓︱break",
                  "🎓︱matchup", "🎓︱ballot", "📊︱poll", "📊︱yes-no-voting"],
        "voice": ["🏟️︱GRAND AUDITORIUM", "😤︱CLASH-EQUITY ROOM"],
    },
    "🎮︱PLAY": {
        "text":  ["❌︱tic-tac-toe︱⭕", "🤛︱rock✊-paper📰-scissors✌️", "🪙︱toss-coin"],
        "voice": [],
    },
    "ℹ️︱INFORMATION": {
        "text":  ["schedule", "important-forms", "debater-briefing",
                  "judge-briefing", "equity-briefing"],
        "voice": [],
    },
}


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
    """
    Completely wipe the server — all channels, categories, and roles.
    Keeps only: kamla-config channel, @everyone, and the bot's own managed role.
    Adds guild to _building_guilds so the channel guard does not fire during wipe.
    """
    _building_guilds.add(guild.id)

    # Delete every channel/category except kamla-config
    for channel in list(guild.channels):
        if "kamla-config" in channel.name:
            continue
        try:
            await channel.delete(reason="KAMLA server wipe")
            await asyncio.sleep(0.4)
        except Exception:
            pass

    # Delete every role except @everyone and the bot's own managed role
    bot_managed_ids = {r.id for r in guild.me.roles if r.managed}
    protected = {guild.default_role.id} | bot_managed_ids
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.id in protected:
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
            if cfg["administrator"]:
                perms = discord.Permissions(administrator=True)
            else:
                perms = discord.Permissions(
                    view_channel=True, send_messages=True, read_message_history=True,
                    connect=True, speak=True, stream=True, use_voice_activation=True,
                    embed_links=True, attach_files=True, add_reactions=True,
                )
            role = await guild.create_role(
                name=name, color=cfg["color"], hoist=cfg["hoist"],
                permissions=perms, reason="KAMLA tournament setup",
            )
            roles[name] = role
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"[Builder] Failed to create role {name}: {e}")
    return roles


def _bot_ow(guild):
    return _allow("view_channel", "send_messages", "read_message_history",
                  "manage_messages", "embed_links", "attach_files",
                  "connect", "speak", "move_members")


def _build_orgcom_overwrites(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in ORG_ACCESS_ROLES:
        if rn in roles:
            ow[roles[rn]] = _role_ow(admin=True, view=True, send=True)
    for rn in KAMLA_ROLE_NAMES:
        if rn in ORG_ACCESS_ROLES:
            continue
        if rn in roles:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
    return ow


def _build_all_role_overwrites(guild, roles, send=True):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
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
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            is_admin = rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True,
                send_messages=is_admin, manage_messages=is_admin,
                embed_links=is_admin, attach_files=is_admin,
            )
    return ow


def _build_public_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True,
        ),
        guild.me: _bot_ow(guild),
    }


def _build_play_overwrites(guild, roles):
    """PLAY category: every KAMLA role except VISITOR can view/use it."""
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn == "VISITOR":
            if rn in roles:
                ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
            continue
        if rn in roles:
            ow[roles[rn]] = _role_ow(admin=rn in ADMIN_ROLE_NAMES, view=True, send=True)
    return ow


def _build_toss_overwrites(guild, roles):
    """toss-coin channel: visible to everyone but VISITOR; only CAP/TABBY/ORG (+admins) may send."""
    toss_allowed = {"CAP", "TABBY", "ORG"}
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn == "VISITOR":
            if rn in roles:
                ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
            continue
        if rn in roles:
            can_send = rn in toss_allowed or rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=can_send,
            )
    return ow


def _room_category_ow(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True,
            )
    return ow


def _room_text_ow(guild, roles, *, hide_from=(), allow_send=True):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn not in roles:
            continue
        if rn in hide_from:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
        else:
            is_admin = rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=allow_send or is_admin,
                embed_links=True,
                attach_files=True,
            )
    return ow


def _room_voice_ow(guild, roles, *, hide_from=(), no_speak=()):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn not in roles:
            continue
        if rn in hide_from:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False, connect=False)
            continue
        can_speak = rn not in no_speak
        ow[roles[rn]] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=can_speak,
            use_voice_activation=can_speak,
            stream=can_speak,
        )
    return ow


async def build_server(guild, fmt, rooms, timezone, tournament_name, creator_id) -> None:
    _building_guilds.add(guild.id)
    try:
        await _build_server_inner(guild, fmt, rooms, timezone, tournament_name, creator_id)
    finally:
        _building_guilds.discard(guild.id)


async def _build_server_inner(
    guild: discord.Guild,
    fmt: str,
    rooms: int,
    timezone: str,
    tournament_name: str,
    creator_id: int,
) -> None:
    roles = await _create_roles(guild)

    try:
        await guild.edit(name=tournament_name, reason="KAMLA tournament setup")
    except Exception as e:
        print(f"[Builder] Could not rename server: {e}")
    try:
        await guild.me.edit(nick=tournament_name[:32])
    except Exception as e:
        print(f"[Builder] Could not set bot nickname: {e}")

    pub_ow = _build_public_overwrites(guild)
    meet_ch = await guild.create_text_channel("👐🏻︱meet-the-developer", overwrites=pub_ow)
    await guild.create_text_channel("👋🏻︱welcome",              overwrites=pub_ow)
    await guild.create_text_channel("❓︱get-role",             overwrites=pub_ow)
    await guild.create_text_channel("🦧︱how-to-use-this-server", overwrites=pub_ow)

    orgcom_ow = _build_orgcom_overwrites(guild, roles)
    orgcom_cat = await guild.create_category("⚜️︱ORGCOM", overwrites=orgcom_ow)
    settings_ch = await guild.create_text_channel("⚙️︱settings",  category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_text_channel("🏴︱org",      category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_text_channel("📂︱document", category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_voice_channel("ORG",          category=orgcom_cat, overwrites=orgcom_ow)
    await guild.create_voice_channel("Control Room", category=orgcom_cat, overwrites=orgcom_ow)

    assign_ow = _build_orgcom_overwrites(guild, roles)
    assign_cat = await guild.create_category("🛅︱ASSIGN", overwrites=assign_ow)
    for rn in ["ORG", "CAP", "TABBY", "EQUITY", "DEBATER", "VISITOR",
               "INDEPENDENT ADJUDICATOR", "INVITED ADJUDICATOR"]:
        await guild.create_text_channel(
            rn.lower().replace(" ", "-"), category=assign_cat, overwrites=assign_ow
        )
        await asyncio.sleep(0.2)

    ga_all_ow = _build_all_role_overwrites(guild, roles, send=True)
    ga_ann_ow = _build_announce_overwrites(guild, roles)
    ga_cat = await guild.create_category(
        "🏟️︱GRAND AUDITORIUM", overwrites=_build_all_role_overwrites(guild, roles)
    )
    await guild.create_text_channel("📨︱ga-text",      category=ga_cat, overwrites=ga_all_ow)
    await guild.create_text_channel("🎓︱motion",       category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱announcement", category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱break",        category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱matchup",      category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("🎓︱ballot",       category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("📊︱poll",          category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_text_channel("📊︱yes-no-voting", category=ga_cat, overwrites=ga_ann_ow)
    await guild.create_voice_channel(
        "🏟️︱GRAND AUDITORIUM", category=ga_cat,
        overwrites=_build_all_role_overwrites(guild, roles),
    )
    await guild.create_voice_channel(
        "😤︱CLASH-EQUITY ROOM", category=ga_cat,
        overwrites=_build_all_role_overwrites(guild, roles),
    )

    play_ow = _build_play_overwrites(guild, roles)
    play_cat = await guild.create_category("🎮︱PLAY", overwrites=play_ow)
    await guild.create_text_channel("❌︱tic-tac-toe︱⭕", category=play_cat, overwrites=play_ow)
    await guild.create_text_channel("🤛︱rock✊-paper📰-scissors✌️", category=play_cat, overwrites=play_ow)
    await guild.create_text_channel(
        "🪙︱toss-coin", category=play_cat, overwrites=_build_toss_overwrites(guild, roles)
    )

    info_send_ow = _build_announce_overwrites(guild, roles)
    info_cat = await guild.create_category(
        "ℹ️︱INFORMATION", overwrites=_build_all_role_overwrites(guild, roles, send=False)
    )
    for ch_name in ["schedule", "important-forms", "debater-briefing",
                    "judge-briefing", "equity-briefing"]:
        await guild.create_text_channel(ch_name, category=info_cat, overwrites=info_send_ow)
        await asyncio.sleep(0.2)

    previous_cfg = config_manager.get_cached(guild.id)
    cfg_data = {
        "format":              fmt,
        "rooms":               rooms,
        "timezone":            timezone,
        "tournament_name":     tournament_name,
        "created_by":          creator_id,
        "created_at":          discord.utils.utcnow().isoformat(),
        "settings_channel_id": settings_ch.id,
        "locked":              False,
        "onboarding_status":   "approved",
    }
    for key in (
        "installer_id",
        "join_webhook_message_id",
        "onboarding_channel_id",
        "onboarding_message_id",
        "server_icon_uploaded",
        "requested_by",
        "requested_tournament",
        "requested_role",
        "approval_requested_at",
        "approved_by",
        "approved_at",
    ):
        if key in previous_cfg:
            cfg_data[key] = previous_cfg[key]
    await config_manager.set_config(guild, cfg_data)

    await _post_meet_developer(meet_ch)
    await _post_welcome(guild)
    await _post_get_role(guild)
    await _post_how_to_use(guild)
    await _post_settings_panel(settings_ch, cfg_data)

    await _create_rooms(guild, roles, fmt, rooms, start_index=1)


AP_PREP_NAMES = {"GOVT PREP", "OPP PREP"}
BP_PREP_NAMES = {"OG PREP", "OO PREP", "CG PREP", "CO PREP"}
ROOM_CATEGORY_RE = re.compile(r"^ROOM (?P<number>\d{2})$")


async def switch_format_rooms(guild: discord.Guild, new_fmt: str, rooms: int) -> None:
    roles = {r.name: r for r in guild.roles}
    prep_ow = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    adj_ow  = _room_voice_ow(guild, roles, hide_from=("DEBATER", "VISITOR"))
    all_prep = AP_PREP_NAMES | BP_PREP_NAMES

    for i in range(1, rooms + 1):
        cat = discord.utils.get(guild.categories, name=f"ROOM {i:02d}")
        if not cat:
            continue
        for ch in list(cat.voice_channels):
            if ch.name in all_prep or ch.name == "ADJUDICATION":
                try:
                    await ch.delete(reason="KAMLA format switch")
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

        if new_fmt.lower() == "bp":
            for prep in ["OG PREP", "OO PREP", "CG PREP", "CO PREP"]:
                await guild.create_voice_channel(prep, category=cat, overwrites=prep_ow, user_limit=2)
                await asyncio.sleep(0.15)
        else:
            await guild.create_voice_channel("GOVT PREP", category=cat, overwrites=prep_ow, user_limit=3)
            await guild.create_voice_channel("OPP PREP",  category=cat, overwrites=prep_ow, user_limit=3)

        await guild.create_voice_channel("ADJUDICATION", category=cat, overwrites=adj_ow)
        await asyncio.sleep(0.2)


async def _create_rooms(
    guild: discord.Guild,
    roles: dict,
    fmt: str,
    count: int,
    start_index: int = 1,
) -> None:
    cat_ow    = _room_category_ow(guild, roles)
    text_ow   = _room_text_ow(guild, roles, hide_from=("VISITOR",), allow_send=True)
    allin_ow  = _room_text_ow(guild, roles, hide_from=("DEBATER", "VISITOR"), allow_send=False)
    debate_ow = _room_voice_ow(guild, roles, no_speak=("VISITOR",))
    prep_ow   = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    adj_ow    = _room_voice_ow(guild, roles, hide_from=("DEBATER", "VISITOR"))

    for i in range(start_index, start_index + count):
        room_name = f"ROOM {i:02d}"
        cat = await guild.create_category(room_name, overwrites=cat_ow)

        await guild.create_text_channel("timer",  category=cat, overwrites=text_ow)
        await guild.create_text_channel("poi",    category=cat, overwrites=text_ow)
        all_in_ch = await guild.create_text_channel("all-in", category=cat, overwrites=allin_ow)

        await guild.create_voice_channel("DEBATE ROOM", category=cat, overwrites=debate_ow)

        if fmt.lower() == "ap":
            await guild.create_voice_channel("GOVT PREP", category=cat, overwrites=prep_ow, user_limit=3)
            await guild.create_voice_channel("OPP PREP",  category=cat, overwrites=prep_ow, user_limit=3)
        else:
            for prep in ["OG PREP", "OO PREP", "CG PREP", "CO PREP"]:
                await guild.create_voice_channel(prep, category=cat, overwrites=prep_ow, user_limit=2)
                await asyncio.sleep(0.15)

        await guild.create_voice_channel("ADJUDICATION", category=cat, overwrites=adj_ow)

        from allin_cog import AllInView
        await all_in_ch.send(
            embed=discord.Embed(
                title="🔴 Push Back All",
                description="Press the button below to move everyone from prep rooms back to **DEBATE ROOM**.\n\n"
                            "⛔ Text messages are not allowed in this channel.",
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
    view.add_item(discord.ui.Button(label="Developer Contact", style=discord.ButtonStyle.link,
                                    url="https://istiack.ami.bd/#contact"))
    view.add_item(discord.ui.Button(label="Donate", style=discord.ButtonStyle.link,
                                    url="https://istiack.ami.bd/#donate"))
    view.add_item(discord.ui.Button(label="Create Tournament", style=discord.ButtonStyle.link,
                                    url="https://kamla-bot.pages.dev"))

    import pathlib
    logo_path = pathlib.Path(__file__).parent / "m.png"
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            embed.set_image(url="attachment://m.png")
            await channel.send(embed=embed, file=discord.File(f, "m.png"), view=view)
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
            "2. Click the button that matches your role\n"
            "3. Your role will be assigned automatically\n\n"
            "**Roles:**\n"
            "🔵 **Invited Adjudicator** — Officially invited judges\n"
            "🟣 **Independent Adjudicator** — Self-registered judges\n"
            "🩵 **Debater** — Competing speakers\n"
            "⬜ **Visitor** — Observers and guests\n\n"
            "Admin roles (ORG/CAP/TABBY/EQUITY) are assigned by organisers only."
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
            "You may only hold **one role** at a time — your previous role will be removed automatically.\n\n"
            "⚠️ If you are **ORG / CAP / TABBY / EQUITY**, do **not** click here — "
            "contact the tournament ORG to change your role."
        ),
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed, view=RoleSelectView())


async def _post_how_to_use(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="🦧︱how-to-use-this-server")
    if not channel:
        return
    embed = discord.Embed(title="🦧 How to Use This Server", color=discord.Color.blurple())
    embed.add_field(name="📌 Channels",
        value="• **welcome** — Read this first\n• **get-role** — Claim your role\n"
              "• **GRAND AUDITORIUM** — Main hub\n• **INFORMATION** — Official documents\n"
              "• **ROOM XX** — Individual debate rooms", inline=False)
    embed.add_field(name="⏱️ Timer",
        value="In **#timer**, send a time: `10m` `1h` `15s` `1h10m` `10m30s`\n"
              "Only lowercase d/h/m/s — no symbols or spaces.", inline=False)
    embed.add_field(name="🙋 POI",
        value="In **#poi**, type `poi` (any case) to request a Point of Information.",
        inline=False)
    embed.add_field(name="🔴 All-In",
        value="In **#all-in**, only **ORG / CAP / TABBY / Invited / Independent Adjudicator** "
              "can press **Push Back All** to pull everyone back from prep to the debate room. "
              "Text messages are not allowed.",
        inline=False)
    await channel.send(embed=embed)


async def _post_settings_panel(channel: discord.TextChannel, cfg: dict) -> None:
    from settings_cog import build_settings_embed, SettingsView
    embed = build_settings_embed(cfg)
    await channel.send(embed=embed, view=SettingsView())


# ── Channel / Category Restoration ────────────────────────────────────────────

def _room_number(category_name: str) -> int | None:
    match = ROOM_CATEGORY_RE.fullmatch(category_name)
    return int(match.group("number")) if match else None


def _room_channel_names(fmt: str) -> tuple[list[str], list[str]]:
    text = ["timer", "poi", "all-in"]
    prep = ["GOVT PREP", "OPP PREP"] if fmt.lower() == "ap" else [
        "OG PREP", "OO PREP", "CG PREP", "CO PREP"
    ]
    return text, ["DEBATE ROOM", *prep, "ADJUDICATION"]


async def _saved_config(guild: discord.Guild) -> dict:
    cfg = config_manager.get_cached(guild.id)
    if cfg:
        return cfg
    try:
        return await asyncio.wait_for(config_manager.get_config(guild), timeout=3.0)
    except Exception:
        return {}


async def restore_if_protected(
    guild: discord.Guild,
    channel_name: str,
    is_category: bool,
    parent_category_name: str | None = None,
) -> bool:
    """
    Called from on_guild_channel_delete.
    Returns True if the channel/category was protected and has been restored.
    Runs in the background — fire-and-forget via asyncio.create_task.
    """
    _restoring_guilds.add(guild.id)
    try:
        if is_category:
            if channel_name in PROTECTED_CATEGORIES:
                await asyncio.sleep(1)
                await _restore_category(guild, channel_name)
                await normalize_kamla_order(guild)
                return True

            room_index = _room_number(channel_name)
            cfg = await _saved_config(guild) if room_index else {}
            if room_index and room_index <= int(cfg.get("rooms", 0)):
                await asyncio.sleep(1)
                await _restore_room_category(guild, room_index)
                await normalize_kamla_order(guild)
                return True
            return False

        if channel_name in PROTECTED_TOP_LEVEL_CHANNELS:
            await asyncio.sleep(1)
            await _restore_top_level_channel(guild, channel_name)
            await normalize_kamla_order(guild)
            return True

        # Restore channels inside the fixed KAMLA categories.
        for cat_name, structure in CATEGORY_CHANNEL_STRUCTURE.items():
            if channel_name in structure["text"] or channel_name in structure["voice"]:
                cat = discord.utils.get(guild.categories, name=cat_name)
                if cat is None:
                    return False
                await asyncio.sleep(1)
                roles = {r.name: r for r in guild.roles}
                is_voice = channel_name in structure["voice"]
                await _restore_single_channel(guild, roles, cat, cat_name, channel_name, is_voice)
                await normalize_kamla_order(guild)
                return True

        # Restore a missing channel inside ROOM XX.
        room_index = _room_number(parent_category_name or "")
        cfg = await _saved_config(guild) if room_index else {}
        if room_index and room_index <= int(cfg.get("rooms", 0)):
            text_names, voice_names = _room_channel_names(cfg.get("format", "ap"))
            if channel_name in text_names or channel_name in voice_names:
                cat = discord.utils.get(guild.categories, name=parent_category_name)
                if cat is None:
                    await _restore_room_category(guild, room_index)
                else:
                    roles = {r.name: r for r in guild.roles}
                    await _restore_room_channel(
                        guild, roles, cat, channel_name, cfg.get("format", "ap")
                    )
                await normalize_kamla_order(guild)
                return True
        return False
    finally:
        _restoring_guilds.discard(guild.id)


async def _restore_category(guild: discord.Guild, cat_name: str) -> None:
    """Recreate a protected category and ALL its missing channels."""
    roles = {r.name: r for r in guild.roles}

    # Compute the right overwrites for this category
    ow = _category_overwrites(guild, roles, cat_name)

    # Check if it already got recreated while we were sleeping
    existing_cat = discord.utils.get(guild.categories, name=cat_name)
    if existing_cat is None:
        try:
            existing_cat = await guild.create_category(cat_name, overwrites=ow)
            print(f"[Guard] Recreated category: {cat_name}")
        except Exception as e:
            print(f"[Guard] Failed to recreate category {cat_name}: {e}")
            return

    structure = CATEGORY_CHANNEL_STRUCTURE.get(cat_name, {"text": [], "voice": []})
    existing_names = {ch.name for ch in existing_cat.channels}

    for ch_name in structure["text"]:
        if ch_name not in existing_names:
            await _restore_single_channel(guild, roles, existing_cat, cat_name, ch_name, False)
            await asyncio.sleep(0.3)

    for ch_name in structure["voice"]:
        if ch_name not in existing_names:
            await _restore_single_channel(guild, roles, existing_cat, cat_name, ch_name, True)
            await asyncio.sleep(0.3)

    # Re-post content for channels that need it
    await _repost_channel_content(guild, cat_name, existing_cat)


async def _restore_top_level_channel(guild: discord.Guild, ch_name: str) -> None:
    """Recreate a protected top-level (no-category) channel."""
    existing = discord.utils.get(guild.text_channels, name=ch_name)
    if existing:
        return  # already back

    pub_ow = _build_public_overwrites(guild)
    try:
        new_ch = await guild.create_text_channel(ch_name, overwrites=pub_ow)
        print(f"[Guard] Recreated top-level channel: {ch_name}")
    except Exception as e:
        print(f"[Guard] Failed to recreate channel {ch_name}: {e}")
        return

    # Re-post embedded content
    if ch_name == "👐🏻︱meet-the-developer":
        await _post_meet_developer(new_ch)
    elif ch_name == "👋🏻︱welcome":
        await _post_welcome(guild)
    elif ch_name == "❓︱get-role":
        await _post_get_role(guild)
    elif ch_name == "🦧︱how-to-use-this-server":
        await _post_how_to_use(guild)


async def _restore_single_channel(
    guild: discord.Guild,
    roles: dict,
    cat: discord.CategoryChannel,
    cat_name: str,
    ch_name: str,
    is_voice: bool,
) -> None:
    """Recreate one channel inside a protected category."""
    existing = discord.utils.get(
        cat.voice_channels if is_voice else cat.text_channels, name=ch_name
    )
    if existing:
        return  # already back

    ow = _channel_overwrites(guild, roles, cat_name, ch_name)
    try:
        if is_voice:
            await guild.create_voice_channel(ch_name, category=cat, overwrites=ow)
        else:
            new_ch = await guild.create_text_channel(ch_name, category=cat, overwrites=ow)
            # Re-post settings panel if it's the settings channel
            if ch_name == "⚙️︱settings":
                from config import config_manager
                cfg = config_manager.get_cached(guild.id) or {}
                if cfg:
                    await _post_settings_panel(new_ch, cfg)
        print(f"[Guard] Recreated channel #{ch_name} in {cat_name}")
    except Exception as e:
        print(f"[Guard] Failed to recreate channel {ch_name}: {e}")


async def _restore_room_category(guild: discord.Guild, room_index: int) -> None:
    """Recreate one ROOM category with the saved AP/BP channel layout."""
    cfg = await _saved_config(guild)
    fmt = cfg.get("format", "ap")
    roles = {r.name: r for r in guild.roles}
    room_name = f"ROOM {room_index:02d}"

    existing = discord.utils.get(guild.categories, name=room_name)
    if existing is None:
        await _create_rooms(guild, roles, fmt, 1, start_index=room_index)
        print(f"[Guard] Recreated category: {room_name}")
        return

    text_names, voice_names = _room_channel_names(fmt)
    for channel_name in text_names:
        await _restore_room_channel(guild, roles, existing, channel_name, fmt)
        await asyncio.sleep(0.2)
    for channel_name in voice_names:
        await _restore_room_channel(guild, roles, existing, channel_name, fmt)
        await asyncio.sleep(0.2)


async def _restore_room_channel(
    guild: discord.Guild,
    roles: dict,
    category: discord.CategoryChannel,
    channel_name: str,
    fmt: str,
) -> None:
    """Recreate one missing channel in a ROOM category."""
    text_names, voice_names = _room_channel_names(fmt)
    is_voice = channel_name in voice_names
    existing = discord.utils.get(
        category.voice_channels if is_voice else category.text_channels,
        name=channel_name,
    )
    if existing:
        return

    if channel_name == "all-in":
        overwrites = _room_text_ow(
            guild, roles, hide_from=("DEBATER", "VISITOR"), allow_send=False
        )
    elif is_voice:
        if channel_name == "DEBATE ROOM":
            overwrites = _room_voice_ow(guild, roles, no_speak=("VISITOR",))
        elif channel_name == "ADJUDICATION":
            overwrites = _room_voice_ow(
                guild, roles, hide_from=("DEBATER", "VISITOR")
            )
        else:
            overwrites = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    else:
        overwrites = _room_text_ow(
            guild, roles, hide_from=("VISITOR",), allow_send=True
        )

    try:
        if is_voice:
            user_limit = 3 if fmt.lower() == "ap" else 2
            await guild.create_voice_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                user_limit=user_limit if "PREP" in channel_name else 0,
            )
        else:
            new_channel = await guild.create_text_channel(
                channel_name, category=category, overwrites=overwrites
            )
            if channel_name == "all-in":
                from allin_cog import AllInView
                await new_channel.send(
                    embed=discord.Embed(
                        title="🔴 Push Back All",
                        description=(
                            "Press the button below to move everyone from prep rooms "
                            "back to **DEBATE ROOM**.\n\n"
                            "⛔ Text messages are not allowed in this channel."
                        ),
                        color=discord.Color.red(),
                    ),
                    view=AllInView(),
                )
        print(f"[Guard] Recreated channel #{channel_name} in {category.name}")
    except Exception as e:
        print(f"[Guard] Failed to recreate room channel {channel_name}: {e}")


async def normalize_kamla_order(guild: discord.Guild) -> None:
    """
    Put KAMLA's categories, room categories, and their child channels back into
    the canonical order. User-created channels/categories are left untouched.
    """
    desired_category_names = [
        "⚜️︱ORGCOM",
        "🛅︱ASSIGN",
        "🏟️︱GRAND AUDITORIUM",
        "🎮︱PLAY",
        "ℹ️︱INFORMATION",
    ]
    rooms = sorted(
        (
            (room_index, category)
            for category in guild.categories
            if (room_index := _room_number(category.name)) is not None
        ),
        key=lambda item: item[0],
    )
    desired_categories: list[discord.CategoryChannel] = []
    for name in desired_category_names:
        category = discord.utils.get(guild.categories, name=name)
        if category:
            desired_categories.append(category)
    desired_categories.extend(category for _, category in rooms)

    # Discord positions are global; moving in reverse order avoids later moves
    # shifting already placed categories.
    for position, category in reversed(list(enumerate(desired_categories))):
        try:
            await category.edit(position=position, reason="KAMLA canonical channel order")
        except (discord.Forbidden, discord.HTTPException):
            continue

    for category in desired_categories:
        if category.name in CATEGORY_CHANNEL_STRUCTURE:
            structure = CATEGORY_CHANNEL_STRUCTURE[category.name]
            ordered_names = [*structure["text"], *structure["voice"]]
        else:
            cfg = await _saved_config(guild)
            text_names, voice_names = _room_channel_names(cfg.get("format", "ap"))
            ordered_names = [*text_names, *voice_names]

        current = {channel.name: channel for channel in category.channels}
        ordered_channels = [current[name] for name in ordered_names if name in current]
        for position, channel in reversed(list(enumerate(ordered_channels))):
            try:
                await channel.edit(
                    position=position,
                    reason="KAMLA canonical channel order",
                )
            except (discord.Forbidden, discord.HTTPException):
                continue


def _category_overwrites(guild: discord.Guild, roles: dict, cat_name: str) -> dict:
    if cat_name in ("⚜️︱ORGCOM", "🛅︱ASSIGN"):
        return _build_orgcom_overwrites(guild, roles)
    elif cat_name == "🏟️︱GRAND AUDITORIUM":
        return _build_all_role_overwrites(guild, roles)
    elif cat_name == "🎮︱PLAY":
        return _build_play_overwrites(guild, roles)
    elif cat_name == "ℹ️︱INFORMATION":
        return _build_all_role_overwrites(guild, roles, send=False)
    return {}


def _channel_overwrites(guild: discord.Guild, roles: dict, cat_name: str, ch_name: str) -> dict:
    if cat_name in ("⚜️︱ORGCOM", "🛅︱ASSIGN"):
        return _build_orgcom_overwrites(guild, roles)
    elif cat_name == "🏟️︱GRAND AUDITORIUM":
        announce_channels = ["🎓︱motion", "🎓︱announcement", "🎓︱break",
                             "🎓︱matchup", "🎓︱ballot", "📊︱poll", "📊︱yes-no-voting"]
        if ch_name in announce_channels:
            return _build_announce_overwrites(guild, roles)
        return _build_all_role_overwrites(guild, roles, send=True)
    elif cat_name == "🎮︱PLAY":
        if ch_name == "🪙︱toss-coin":
            return _build_toss_overwrites(guild, roles)
        return _build_play_overwrites(guild, roles)
    elif cat_name == "ℹ️︱INFORMATION":
        return _build_announce_overwrites(guild, roles)
    return {}


async def _repost_channel_content(
    guild: discord.Guild,
    cat_name: str,
    cat: discord.CategoryChannel,
) -> None:
    """Re-post embedded bot messages in channels that need them after restoration."""
    if cat_name == "🛅︱ASSIGN":
        pass  # Assign channels don't need initial content
    elif cat_name == "⚜️︱ORGCOM":
        settings_ch = discord.utils.get(cat.text_channels, name="⚙️︱settings")
        if settings_ch:
            from config import config_manager
            cfg = config_manager.get_cached(guild.id) or {}
            if cfg:
                try:
                    # Only post if the channel is empty
                    history = [m async for m in settings_ch.history(limit=1)]
                    if not history:
                        await _post_settings_panel(settings_ch, cfg)
                except Exception:
                    pass
