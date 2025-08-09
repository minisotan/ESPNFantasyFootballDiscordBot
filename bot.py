# bot.py
import os
import asyncio
from collections import defaultdict
import discord
from discord.ext import commands
from discord.ui import Button, View, Select
from discord import app_commands, Embed
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from espn_api.football import League
from settings_manager import (
    get_guild_settings,
    set_guild_settings,
    set_autopost,
    get_discord_bot_token,
    init_db
)

# ---------- Discord setup ----------
intents = discord.Intents.default()  # Slash-command bot doesn't need message_content
bot = commands.Bot(command_prefix="!", intents=intents)

# Scheduler in ET (DST-aware): Tuesdays @ 11:00 AM
scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/New_York"))

# ---------- Global ESPN concurrency gate ----------
ESPN_MAX_CONCURRENCY = int(os.getenv("ESPN_MAX_CONCURRENCY", "1"))       # how many ESPN calls at once
ESPN_TIMEOUT_SECONDS = int(os.getenv("ESPN_TIMEOUT_SECONDS", "25"))       # per-call timeout
_ESPN_GATE = asyncio.Semaphore(ESPN_MAX_CONCURRENCY)

async def espn_call(func, *args, **kwargs):
    """
    Run a blocking espn_api call in a worker thread with:
      - global concurrency limit (queue instead of fail)
      - timeout guard
    """
    async with _ESPN_GATE:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=ESPN_TIMEOUT_SECONDS
        )

# Per-guild lock to keep one /weeklyrecap per server at a time (optional but nice)
_guild_locks = defaultdict(asyncio.Lock)

# ---- Global job queue (all guilds share this) ----
_GLOBAL_QUEUE: asyncio.Queue[discord.Interaction] = asyncio.Queue()
_GLOBAL_WORKERS: list[asyncio.Task] = []

# Still keep a per-guild lock so two jobs from the SAME server don't overlap
from collections import defaultdict
_GUILD_LOCKS = defaultdict(asyncio.Lock)

# How many jobs to process in parallel (separate from ESPN_MAX_CONCURRENCY)
QUEUE_WORKERS = int(os.getenv("QUEUE_WORKERS", "1"))  # 1 = strict FIFO; >1 = parallel consumption

# ---------- ESPN image/constants ----------
PLAYER_IMG = "https://a.espncdn.com/i/headshots/nfl/players/full/{player_id}.png"
TEAM_IMG = "https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"

TEAM_LOGO = {
    "49ers": "sf", "Bears": "chi", "Bengals": "cin", "Bills": "buf", "Broncos": "den",
    "Browns": "cle", "Buccaneers": "tb", "Cardinals": "ari", "Chargers": "lac", "Chiefs": "kc",
    "Colts": "ind", "Commanders": "wsh", "Cowboys": "dal", "Dolphins": "mia", "Eagles": "phi",
    "Falcons": "atl", "Giants": "nyg", "Jaguars": "jax", "Jets": "nyj", "Lions": "det",
    "Packers": "gb", "Panthers": "car", "Patriots": "ne", "Raiders": "lv", "Rams": "lar",
    "Ravens": "bal", "Saints": "no", "Seahawks": "sea", "Steelers": "pit", "Texans": "hou",
    "Titans": "ten", "Vikings": "min"
}
DESIRED_POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'D/ST']

# ---------- Helpers ----------
async def build_league_from_settings(settings) -> League:
    # espn_api does network IO in League(...), so offload it too
    return await espn_call(
        League,
        league_id=int(settings["league_id"]),
        year=int(settings["season"]),
        espn_s2=settings["espn_s2"],
        swid=settings["swid"]
    )

# --- Scoring precision detection (0, 1, or 2 decimal places) ---
_PRECISION_CACHE: dict[int, int] = {}

def _fmt_points(val: float | int | None, precision: int) -> str:
    if val is None:
        return "0"
    return f"{float(val):.{precision}f}"

async def detect_scoring_precision(league) -> int:
    """
    Infer precision by sampling box score values and checking if they
    align to 0, 1, or 2 decimal places. Caches per-league.
    """
    league_id = int(getattr(league, "league_id", 0) or 0)
    if league_id in _PRECISION_CACHE:
        return _PRECISION_CACHE[league_id]

    # Choose up to two weeks to sample: current and week 1 (defensive)
    current_week = (
        int(getattr(league, "current_week", 0) or 0)
        or int(getattr(league, "nfl_week", 0) or 0)
        or 1
    )
    weeks_to_try = [max(1, current_week)]
    if current_week != 1:
        weeks_to_try.append(1)

    samples: list[float] = []
    for wk in weeks_to_try:
        try:
            boxes = await espn_call(league.box_scores, week=wk)
            for g in boxes:
                # team scores
                if g.home_score is not None: samples.append(float(g.home_score))
                if g.away_score is not None: samples.append(float(g.away_score))
                # player points
                for lineup in (g.home_lineup, g.away_lineup):
                    for bp in lineup:
                        pts = getattr(bp, "points", None)
                        if pts is not None:
                            samples.append(float(pts))
        except Exception:
            continue

    # Fallback if we couldn't sample anything
    if not samples:
        _PRECISION_CACHE[league_id] = 2
        return 2

    # Check which precision cleanly represents all samples
    for p in (0, 1, 2):
        mult = 10 ** p
        if all(abs(round(v * mult) - v * mult) < 1e-6 for v in samples):
            _PRECISION_CACHE[league_id] = p
            return p

    _PRECISION_CACHE[league_id] = 2
    return 2

async def _process_weeklyrecap(interaction: discord.Interaction):
    """Runs the actual recap build/send for a single guild request."""
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.followup.send("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    # Target channel (prefer configured channel)
    channel = (
        interaction.guild.get_channel(int(settings["channel_id"]))
        if settings.get("channel_id") else interaction.channel
    )

    # Permission check
    perms = channel.permissions_for(interaction.guild.me)
    if not perms.send_messages or not perms.embed_links:
        await interaction.followup.send(
            f"❌ I don’t have permission to post embeds in {channel.mention}.",
            ephemeral=True
        )
        return

    # Build league + robust current week
    league = await build_league_from_settings(settings)
    current_week = (
        int(getattr(league, "current_week", 0) or 0)
        or int(getattr(league, "nfl_week", 0) or 0)
        or 1
    )
    if current_week < 1:
        current_week = 1

    # Build pages (1..current_week)
    week_pages: list[list[discord.Embed]] = []
    for wk in range(1, current_week + 1):
        try:
            page = await build_week_page(league, wk)
            if page:
                week_pages.append(page)
        except Exception as inner_e:
            print(f"⚠️ Skipping week {wk} due to error: {inner_e}")

    # Fallback to week 1 if nothing
    if not week_pages:
        try:
            fallback = await build_week_page(league, 1)
            if fallback:
                week_pages.append(fallback)
        except Exception as fe:
            print(f"⚠️ Fallback week 1 failed: {fe}")

    if not week_pages:
        await interaction.followup.send("🤷 I couldn’t find any data to post yet.", ephemeral=True)
        return

    # Send with navigator
    view = WeekNavigator(week_pages)
    first_page = week_pages[-1]
    message = await channel.send(embeds=first_page, view=view)
    view.set_message(message)

    await interaction.followup.send(
        f"✅ Weekly recap posted in {channel.mention}.",
        ephemeral=True
    )

def normalize_weekly_embed_heights(embeds: list[discord.Embed]) -> None:
    """Pad weekly-top embeds so they share the same visual height."""
    if not embeds:
        return
    def line_count(e: discord.Embed) -> int:
        desc = e.description or ""
        return desc.count("\n") + (1 if desc else 0)
    max_lines = max(line_count(e) for e in embeds)
    for e in embeds:
        missing = max_lines - line_count(e)
        if missing > 0:
            # zero-width space keeps a visible blank line in Discord
            e.description = (e.description or "") + ("\n\u200b" * missing)

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    _ensure_global_workers()   # <--- start workers
    scheduler.start()
    print(f"✅ Logged in as {bot.user}")

def _ensure_global_workers():
    # Start exactly QUEUE_WORKERS background tasks
    alive = [t for t in _GLOBAL_WORKERS if not t.done()]
    missing = max(0, QUEUE_WORKERS - len(alive))
    for _ in range(missing):
        _GLOBAL_WORKERS.append(asyncio.create_task(_global_worker()))

async def _global_worker():
    while True:
        interaction: discord.Interaction = await _GLOBAL_QUEUE.get()
        try:
            # Per-guild mutex so same guild requests don't overlap
            async with _GUILD_LOCKS[interaction.guild.id]:
                await _process_weeklyrecap(interaction)
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"❌ Error while processing queued recap: `{e}`",
                    ephemeral=True
                )
            except Exception:
                pass
        finally:
            _GLOBAL_QUEUE.task_done()


async def build_weekly_top_embeds(league: League, week: int, precision: int, starters_only: bool = False) -> list[discord.Embed]:
    """Top player per position for a given week using box scores."""
    best = {p: None for p in DESIRED_POSITIONS}
    week_boxes = await espn_call(league.box_scores, week=week)

    for game in week_boxes:
        for lineup, fteam in ((game.home_lineup, game.home_team), (game.away_lineup, game.away_team)):
            for bp in lineup:
                pts = getattr(bp, "points", None)
                base_pos = getattr(bp, "position", None)
                slot_pos = getattr(bp, "slot_position", None)
                if pts is None or base_pos is None:
                    continue
                if starters_only and slot_pos == "BE":
                    continue

                pos = "D/ST" if base_pos in ("DST", "DEF", "Def") else base_pos
                if pos not in DESIRED_POSITIONS:
                    if base_pos in DESIRED_POSITIONS:
                        pos = base_pos
                    else:
                        continue

                current = best.get(pos)
                if current is None or float(pts) > current["points"]:
                    best[pos] = {
                        "name": bp.name,
                        "points": float(pts),
                        "id": getattr(bp, "playerId", None),
                        "team": getattr(fteam, "team_name", "Unknown"),
                    }

    embeds: list[discord.Embed] = []
    for pos in DESIRED_POSITIONS:
        top = best.get(pos)
        if not top:
            continue

        if pos == "D/ST":
            team_key = top["name"].replace(" D/ST", "").strip()
            code = TEAM_LOGO.get(team_key)
            image_url = TEAM_IMG.format(code=code) if code else None
        else:
            image_url = PLAYER_IMG.format(player_id=top["id"]) if top.get("id") else None

        e = Embed(
            title=f"Week {week} Top {pos}",
            description=(
                f"**{top['name']}** ({pos})\n"
                f"Fantasy Points: **{_fmt_points(top['points'], precision)}**\n"
                f"Fantasy Team: *{top['team']}*"
            ),
            color=0x1abc9c
        )
        if image_url:
            e.set_thumbnail(url=image_url)
        embeds.append(e)

    return embeds

async def build_season_top_embed_combined(league: League, end_week: int, precision: int, starters_only: bool = False) -> discord.Embed:
    """Single embed with Top-5 for each position through end_week."""
    season_points: dict[str, dict[str, float]] = {p: {} for p in DESIRED_POSITIONS}
    for wk in range(1, end_week + 1):
        week_boxes = await espn_call(league.box_scores, week=wk)
        for game in week_boxes:
            for lineup in (game.home_lineup, game.away_lineup):
                for bp in lineup:
                    pts = getattr(bp, "points", None)
                    pos = getattr(bp, "position", None)
                    slot = getattr(bp, "slot_position", None)
                    if pts is None or pos is None:
                        continue
                    if starters_only and slot == "BE":
                        continue
                    pos = "D/ST" if pos in ("DST", "DEF", "Def") else pos
                    if pos not in DESIRED_POSITIONS:
                        continue
                    season_points[pos][bp.name] = season_points[pos].get(bp.name, 0.0) + float(pts)

    lines = []
    for pos in DESIRED_POSITIONS:
        top5 = sorted(season_points[pos].items(), key=lambda x: x[1], reverse=True)[:5]
        section = "\n".join(f"• **{name}** — {_fmt_points(pts, precision)}" for name, pts in top5) if top5 else "_No data_"
        lines.append(f"**{pos}**\n{section}")

    # Purple so it’s visually distinct from H2H
    return Embed(
        title=f"Season Top 5 (through Week {end_week})",
        description="\n\n".join(lines),
        color=0x9b59b6
    )

async def build_head_to_head_embed(league: League, week: int, precision: int) -> discord.Embed:
    box_scores = await espn_call(league.box_scores, week=week)
    e = Embed(
        title=f"Week {week} Head-to-Head Matchups",
        description="🏈 Weekly fantasy results",
        color=0xf39c12
    )
    for game in box_scores:
        home, away = game.home_team, game.away_team
        hs, as_ = game.home_score, game.away_score
        hv, av = hasattr(home, "team_name"), hasattr(away, "team_name")

        if hv and av:
            winner = home if hs > as_ else away
            win_score = hs if hs > as_ else as_
            result = (
                f"{home.team_name} ({home.wins}-{home.losses}) vs. {away.team_name} ({away.wins}-{away.losses})\n"
                f"Score: {_fmt_points(hs, precision)} - {_fmt_points(as_, precision)}\n"
                f"🏆 Winner: **{winner.team_name}** (**{_fmt_points(win_score, precision)}**)"
            )
        elif hv:
            result = (
                f"{home.team_name} ({home.wins}-{home.losses}) vs. BYE\n"
                f"Score: {_fmt_points(hs, precision)} - {_fmt_points(0, precision)}\n"
                f"🛌 **{home.team_name}** is on a bye week!"
            )
        elif av:
            result = (
                f"BYE vs. {away.team_name} ({away.wins}-{away.losses})\n"
                f"Score: {_fmt_points(0, precision)} - {_fmt_points(as_, precision)}\n"
                f"🛌 **{away.team_name}** is on a bye week!"
            )
        else:
            continue

        e.add_field(name="Matchup", value=result, inline=False)
    return e

async def build_power_rankings_embed(league: League, precision: int) -> discord.Embed:
    teams = await espn_call(lambda: list(league.teams))
    teams = sorted(
        teams,
        key=lambda t: (-(getattr(t, "wins", 0) or 0), -float(getattr(t, "points_for", 0) or 0.0))
    )
    e = Embed(title="📊 Power Rankings", description="Sorted by Wins, then Points For", color=0x2980b9)
    for i, team in enumerate(teams, 1):
        pf = _fmt_points(getattr(team, "points_for", 0) or 0, precision)
        pa = _fmt_points(getattr(team, "points_against", 0) or 0, precision)
        e.add_field(
            name=f"{i}. {team.team_name.strip()}",
            value=f"Record: {getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)} | PF: {pf} | PA: {pa}",
            inline=False
        )
    return e

async def build_week_page(league: League, week: int) -> list[discord.Embed]:
    """One page for a given week, in this order:
       1) Head-to-head, 2) Weekly Top Players, 3) Season Top-5 (combined), 4) Power Rankings."""
    embeds: list[discord.Embed] = []
    precision = await detect_scoring_precision(league)
    # 1) Head-to-Head
    h2h = await build_head_to_head_embed(league, week, precision)
    embeds.append(h2h)
    # 2) Weekly Top Players
    weekly_top_embeds = await build_weekly_top_embeds(league, week, precision)
    normalize_weekly_embed_heights(weekly_top_embeds)  # <-- add this line
    embeds.extend(weekly_top_embeds)
    # 3) Season Top-5 (combined)
    season_top_embed = await build_season_top_embed_combined(league, week, precision)
    embeds.append(season_top_embed)
    # 4) Power Rankings
    pr = await build_power_rankings_embed(league, precision)
    embeds.append(pr)
    return embeds[:10]  # Discord limit guard

# ---------- Week Navigator ----------
class WeekNavigator(View):
    def __init__(self, week_embeds: list[list[discord.Embed]]):
        super().__init__(timeout=300)
        self.week_embeds = week_embeds
        self.index = len(week_embeds) - 1
        self.message: discord.Message | None = None

        options = [discord.SelectOption(label=f"Week {i+1}", value=str(i)) for i in range(len(week_embeds))]
        self.select = Select(placeholder="Jump to week…", min_values=1, max_values=1, options=options)
        self.select.callback = self.jump_to_week
        self.add_item(self.select)

    def set_message(self, message: discord.Message):
        self.message = message

    def update_button_states(self):
        self.previous.disabled = (self.index == 0)
        self.next.disabled = (self.index == len(self.week_embeds) - 1)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        if self.index > 0:
            self.index -= 1
        self.update_button_states()
        await self.message.edit(embeds=self.week_embeds[self.index], view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        if self.index < len(self.week_embeds) - 1:
            self.index += 1
        self.update_button_states()
        await self.message.edit(embeds=self.week_embeds[self.index], view=self)

    @discord.ui.button(label="⏹ Reset", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        self.index = len(self.week_embeds) - 1
        self.update_button_states()
        await self.message.edit(embeds=self.week_embeds[self.index], view=self)

    async def jump_to_week(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.index = int(self.select.values[0])
        self.update_button_states()
        await self.message.edit(embeds=self.week_embeds[self.index], view=self)

# ---------- Commands ----------
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    scheduler.start()
    print(f"✅ Logged in as {bot.user}")

@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="setup", description="Configure league for this server")
@app_commands.describe(
    league_id="ESPN league id (number)",
    season="Season year (e.g., 2024)",
    swid="Your ESPN SWID cookie (including braces)",
    espn_s2="Your ESPN S2 cookie",
    channel="Channel where weekly recaps will be posted"
)
async def setup(
    interaction: discord.Interaction,
    league_id: int,
    season: int,
    swid: str,
    espn_s2: str,
    channel: discord.TextChannel
):
    await interaction.response.defer(ephemeral=True)
    # Validate cookies/league up front so we don't save bad creds
    try:
        test_league = await espn_call(
            League, league_id=int(league_id), year=int(season), swid=swid, espn_s2=espn_s2
        )
        _ = await espn_call(lambda: list(test_league.teams))
    except Exception as e:
        await interaction.followup.send(
            "❌ Those cookies don’t grant access to this league. "
            "Make sure they’re copied from an account in the league and that ESPN_S2 is not URL-encoded.\n"
            f"Error: `{e}`",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    await set_guild_settings(
        guild_id,
        league_id=str(league_id),
        season=str(season),
        swid=swid,
        espn_s2=espn_s2,
        channel_id=str(channel.id)
    )
    await interaction.followup.send("✅ Setup complete and validated!", ephemeral=True)

@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="configure", description="Update existing league settings")
@app_commands.describe(
    league_id="(Optional) ESPN league id",
    season="(Optional) Season year",
    swid="(Optional) ESPN SWID cookie",
    espn_s2="(Optional) ESPN S2 cookie",
    channel="(Optional) Post channel"
)
async def configure(
    interaction: discord.Interaction,
    league_id: int | None = None,
    season: int | None = None,
    swid: str | None = None,
    espn_s2: str | None = None,
    channel: discord.TextChannel | None = None
):
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild.id)
    current = await get_guild_settings(guild_id)
    if not current:
        await interaction.followup.send("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    updated = {
        "league_id": str(league_id) if league_id else current["league_id"],
        "season": str(season) if season else current["season"],
        "swid": swid or current["swid"],
        "espn_s2": espn_s2 or current["espn_s2"],
        "channel_id": str(channel.id) if channel else current["channel_id"]
    }
    await set_guild_settings(guild_id, **updated)
    await interaction.followup.send("✅ Settings updated successfully!", ephemeral=True)

@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="autopost", description="Enable or disable automatic weekly recaps")
@app_commands.describe(enabled="Set to true to enable, false to disable")
async def autopost(interaction: discord.Interaction, enabled: bool):
    await interaction.response.defer(ephemeral=True)
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.followup.send("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    await set_autopost(str(interaction.guild.id), enabled)
    msg = (
        "✅ Auto-posting enabled! Weekly recaps will post Tuesdays at 11:00 AM ET."
        if enabled else "❌ Auto-posting disabled."
    )
    await interaction.followup.send(msg, ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(name="show_settings", description="Show saved league settings for this server (admin only).")
@app_commands.default_permissions(manage_guild=True)
async def show_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    s = await get_guild_settings(str(interaction.guild.id))
    if not s:
        await interaction.followup.send("No settings saved yet. Use `/setup`.", ephemeral=True)
        return
    msg = (
        f"League ID: {s['league_id']}\n"
        f"Season: {s['season']}\n"
        f"Channel: <#{s['channel_id']}>\n"
        f"Autopost: {'Enabled' if s.get('autopost_enabled') else 'Disabled'}"
    )
    await interaction.followup.send(msg, ephemeral=True)

# Cooldown feedback
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        try:
            await interaction.response.send_message(
                f"⏳ Cooldown: try again in {error.retry_after:.1f}s.",
                ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                f"⏳ Cooldown: try again in {error.retry_after:.1f}s.",
                ephemeral=True
            )

# Weekly recap (with per-guild lock)
@app_commands.guild_only()
@bot.tree.command(name="weeklyrecap", description="Manually trigger a weekly recap")
async def weeklyrecap_slash(interaction: discord.Interaction):
    # Ack immediately so Discord doesn't time out
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Quick preflight (fail fast before queuing)
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.followup.send("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    channel = (
        interaction.guild.get_channel(int(settings["channel_id"]))
        if settings.get("channel_id") else interaction.channel
    )
    perms = channel.permissions_for(interaction.guild.me)
    if not perms.send_messages or not perms.embed_links:
        await interaction.followup.send(
            f"❌ I don’t have permission to post embeds in {channel.mention}.",
            ephemeral=True
        )
        return

    # Enqueue to the global queue and show GLOBAL position
    position = _GLOBAL_QUEUE.qsize() + 1  # 1-based position after this enqueue
    await _GLOBAL_QUEUE.put(interaction)
    _ensure_global_workers()

    # If you run multiple workers, make that clear in the message
    parallel = " (processing up to "
    parallel += f"{QUEUE_WORKERS} at a time)" if QUEUE_WORKERS > 1 else ")"
    await interaction.followup.send(
        f"Your Weekly Recap is being processed. Please wait while we gather data...",
        ephemeral=True
    )


@bot.tree.command(name="debug_week", description="Show detected current week values")
@app_commands.guild_only()
async def debug_week(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.followup.send("No settings found.", ephemeral=True)
        return
    league = await build_league_from_settings(settings)
    cw = getattr(league, "current_week", None)
    nw = getattr(league, "nfl_week", None)
    await interaction.followup.send(f"current_week={cw!r}, nfl_week={nw!r}", ephemeral=True)

# ---------- Scheduler (auto-post Tuesdays 11:00 AM ET) ----------
@scheduler.scheduled_job("cron", day_of_week="tue", hour=11, minute=0)
async def auto_post_weekly_recap():
    for guild in bot.guilds:
        try:
            settings = await get_guild_settings(str(guild.id))
            if not settings or not settings.get("autopost_enabled") or not settings.get("channel_id"):
                continue

            channel = guild.get_channel(int(settings["channel_id"]))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue

            league = await build_league_from_settings(settings)
            week = (
                int(getattr(league, "current_week", 0) or 0)
                or int(getattr(league, "nfl_week", 0) or 0)
                or 1
            )
            if week < 1:
                week = 1

            page = await build_week_page(league, week)
            if not page:
                await channel.send(f"🤷 No data available for week {week} yet.")
            else:
                await channel.send(embeds=page)

        except Exception as e:
            print(f"❌ Auto-post failed for guild {guild.id}: {e}")

# ---------- Entrypoint ----------
if __name__ == "__main__":
    asyncio.run(bot.start(get_discord_bot_token()))
