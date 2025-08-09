# bot.py
import asyncio
import discord
from collections import defaultdict
_guild_locks = defaultdict(asyncio.Lock)
from discord.ext import commands
from discord.ui import Button, View, Select
from discord import app_commands, Embed
from discord.app_commands import checks
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
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/New_York"))  # ET (DST aware)

# ---------- ESPN image/constants ----------
PLAYER_IMG = "https://a.espncdn.com/combiner/i?img=/i/headshots/nfl/players/full/{player_id}.png&w=200&h=200"
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
def build_league_from_settings(settings) -> League:
    return League(
        league_id=int(settings["league_id"]),
        year=int(settings["season"]),
        espn_s2=settings["espn_s2"],
        swid=settings["swid"]
    )

@bot.tree.command(name="show_settings", description="Show saved league settings for this server (admin only).")
async def show_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    s = await get_guild_settings(str(interaction.guild.id))
    if not s:
        await interaction.followup.send("No settings saved yet. Use `/setup`.", ephemeral=True)
        return
    # Don’t echo cookies back; just confirm presence.
    msg = (
        f"League ID: {s['league_id']}\n"
        f"Season: {s['season']}\n"
        f"Channel: <#{s['channel_id']}>\n"
        f"Autopost: {'Enabled' if s.get('autopost_enabled') else 'Disabled'}"
    )
    await interaction.followup.send(msg, ephemeral=True)

async def build_weekly_top_embeds(league: League, week: int, starters_only: bool = False) -> list[discord.Embed]:
    """Top player per position for a given week using box scores."""
    best: dict[str, dict | None] = {p: None for p in DESIRED_POSITIONS}
    week_boxes = await asyncio.to_thread(league.box_scores, week=week)

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
                f"Fantasy Points: **{top['points']:.2f}**\n"
                f"Fantasy Team: *{top['team']}*"
            ),
            color=0x1abc9c
        )
        if image_url:
            e.set_thumbnail(url=image_url)
        embeds.append(e)

    return embeds


async def build_season_top_embed_combined(league: League, end_week: int, starters_only: bool = False) -> discord.Embed:
    """Single embed with Top-5 for each position through end_week."""
    season_points: dict[str, dict[str, float]] = {p: {} for p in DESIRED_POSITIONS}
    for wk in range(1, end_week + 1):
        week_boxes = await asyncio.to_thread(league.box_scores, week=wk)
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
        section = "\n".join(f"• **{name}** — {pts:.2f}" for name, pts in top5) if top5 else "_No data_"
        lines.append(f"**{pos}**\n{section}")

    return Embed(
        title=f"Season Top 5 (through Week {end_week})",
        description="\n\n".join(lines),
        color=0xe67e22
    )


async def build_head_to_head_embed(league: League, week: int) -> discord.Embed:
    box_scores = await asyncio.to_thread(league.box_scores, week=week)
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
            win_score = max(hs, as_)
            result = (
                f"{home.team_name} ({home.wins}-{home.losses}) vs. {away.team_name} ({away.wins}-{away.losses})\n"
                f"Score: {hs:.1f} - {as_:.1f}\n"
                f"🏆 Winner: **{winner.team_name}** (**{win_score:.1f}**)"
            )
        elif hv:
            result = (
                f"{home.team_name} ({home.wins}-{home.losses}) vs. BYE\n"
                f"Score: {hs:.1f} - 0.0\n"
                f"🛌 **{home.team_name}** is on a bye week!"
            )
        elif av:
            result = (
                f"BYE vs. {away.team_name} ({away.wins}-{away.losses})\n"
                f"Score: 0.0 - {as_:.1f}\n"
                f"🛌 **{away.team_name}** is on a bye week!"
            )
        else:
            continue
        e.add_field(name="Matchup", value=result, inline=False)
    return e

async def build_power_rankings_embed(league: League) -> discord.Embed:
    teams = await asyncio.to_thread(lambda: list(league.teams))
    teams = sorted(
        teams,
        key=lambda t: (-(getattr(t, "wins", 0) or 0), -float(getattr(t, "points_for", 0) or 0.0))
    )
    e = Embed(title="📊 Power Rankings", description="Sorted by Wins, then Points For", color=0x2980b9)
    for i, team in enumerate(teams, 1):
        e.add_field(
            name=f"{i}. {team.team_name.strip()}",
            value=(
                f"Record: {getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)} | "
                f"PF: {float(getattr(team, 'points_for', 0) or 0):.1f} | "
                f"PA: {float(getattr(team, 'points_against', 0) or 0):.1f}"
            ),
            inline=False,
        )
    return e


async def build_week_page(league: League, week: int) -> list[discord.Embed]:
    """One page for a given week, in this order:
       1) Head-to-head, 2) Weekly Top Players, 3) Season Top-5 (combined), 4) Power Rankings."""
    embeds: list[discord.Embed] = []
    # 1) Head-to-Head FIRST
    h2h = await build_head_to_head_embed(league, week)
    embeds.append(h2h)
    # 2) Weekly Top Players
    weekly_top_embeds = await build_weekly_top_embeds(league, week)
    embeds.extend(weekly_top_embeds)
    # 3) Season Top-5 (combined) THROUGH selected week
    season_top_embed = await build_season_top_embed_combined(league, week)
    embeds.append(season_top_embed)
    # 4) Power Rankings
    pr = await build_power_rankings_embed(league)
    embeds.append(pr)
    # Safety: Discord max 10 embeds/message
    return embeds[:10]


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
        # Toggle the actual button attributes (not children indices)
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
    guild_id = str(interaction.guild.id)
    await set_guild_settings(
        guild_id,
        league_id=str(league_id),
        season=str(season),
        swid=swid,
        espn_s2=espn_s2,
        channel_id=str(channel.id)
    )
    await interaction.response.send_message("✅ Setup complete!", ephemeral=True)

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
    guild_id = str(interaction.guild.id)
    current = await get_guild_settings(guild_id)
    if not current:
        await interaction.response.send_message("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    updated = {
        "league_id": str(league_id) if league_id else current["league_id"],
        "season": str(season) if season else current["season"],
        "swid": swid or current["swid"],
        "espn_s2": espn_s2 or current["espn_s2"],
        "channel_id": str(channel.id) if channel else current["channel_id"]
    }
    await set_guild_settings(guild_id, **updated)
    await interaction.response.send_message("✅ Settings updated successfully!", ephemeral=True)


@bot.tree.command(name="autopost", description="Enable or disable automatic weekly recaps")
@app_commands.describe(enabled="Set to true to enable, false to disable")
async def autopost(interaction: discord.Interaction, enabled: bool):
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.response.send_message("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    await set_autopost(str(interaction.guild.id), enabled)
    msg = (
        "✅ Auto-posting enabled! Weekly recaps will post Tuesdays at 11:00 AM ET."
        if enabled else "❌ Auto-posting disabled."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@checks.cooldown(1, 30.0)
@bot.tree.command(name="weeklyrecap", description="Manually trigger a weekly recap")
async def weeklyrecap_slash(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    lock = _guild_locks[interaction.guild.id]
    async with lock:
        try:
            settings = await get_guild_settings(str(interaction.guild.id))
            if not settings:
                await interaction.followup.send("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
                return

            # Prefer configured channel; fallback to where the command was used
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
            league = build_league_from_settings(settings)
            current_week = (
                int(getattr(league, "current_week", 0) or 0)
                or int(getattr(league, "nfl_week", 0) or 0)
                or 1
            )
            if current_week < 1:
                current_week = 1

            # Build pages (one per week)
            week_pages: list[list[discord.Embed]] = []
            for wk in range(1, current_week + 1):
                try:
                    page = await build_week_page(league, wk)
                    if page:
                        week_pages.append(page)
                except Exception as inner_e:
                    print(f"⚠️ Skipping week {wk} due to error: {inner_e}")

            # If nothing built (preseason/empty), try week 1
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

            view = WeekNavigator(week_pages)
            first_page = week_pages[-1]
            message = await channel.send(embeds=first_page, view=view)
            view.set_message(message)

            await interaction.followup.send(
                f"✅ Weekly recap posted in {channel.mention} with week navigation (current week: {current_week}).",
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error while posting: `{e}`", ephemeral=True)


@bot.tree.command(name="debug_week", description="Show detected current week values")
async def debug_week(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = await get_guild_settings(str(interaction.guild.id))
    if not settings:
        await interaction.followup.send("No settings found.", ephemeral=True)
        return
    league = build_league_from_settings(settings)
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

            league = build_league_from_settings(settings)
            week = (
                int(getattr(league, "current_week", 0) or 0)
                or int(getattr(league, "nfl_week", 0) or 0)
                or 1
            )
            if week < 1:
                week = 1

            page = await build_week_page(league, week)  # one message, ≤10 embeds
            if not page:
                await channel.send(f"🤷 No data available for week {week} yet.")
            else:
                await channel.send(embeds=page)

        except Exception as e:
            print(f"❌ Auto-post failed for guild {guild.id}: {e}")


# ---------- Entrypoint ----------
if __name__ == "__main__":
    token = get_discord_bot_token()
    asyncio.run(bot.start(token))
