# bot.py
import asyncio
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

async def send_weekly_recap_to_channel(guild: discord.Guild, channel: discord.abc.Messageable):
    """Builds all recap embeds and sends them to the given channel."""
    # Ensure channel is messageable and we have perms
    try:
        perms = channel.permissions_for(guild.me)
        if not perms.send_messages:
            await channel.send  # just to force attribute access for typing; no-op
    except Exception:
        # Fallback: try configured channel if passed channel isn't usable
        settings = await get_guild_settings(str(guild.id))
        if settings and settings.get("channel_id"):
            fallback = guild.get_channel(int(settings["channel_id"]))
            if fallback and fallback != channel:
                channel = fallback
    settings = await get_guild_settings(str(guild.id))
    if not settings:
        await channel.send("❌ This server hasn't been set up. Use `/setup` first.")
        return

    try:
        league = build_league_from_settings(settings)
    except Exception as e:
        await channel.send(f"❌ Failed to initialize ESPN League: {e}")
        return

    # Pick a week (fallback-safe)
    week = getattr(league, "current_week", None) or getattr(league, "nfl_week", None) or 1

    embeds: list[discord.Embed] = []

    # Weekly top players
    try:
        weekly_top_embeds = await build_weekly_top_embeds(league, int(week))
        pad_embeds(weekly_top_embeds)
        embeds.extend(weekly_top_embeds)
    except Exception as e:
        print(f"❌ Failed weekly tops w{week}: {e}")

    # Head-to-head
    try:
        box_scores = league.box_scores(week=int(week))
        matchup_embed = Embed(
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

            matchup_embed.add_field(name="Matchup", value=result, inline=False)

        embeds.append(matchup_embed)
    except Exception as e:
        print(f"❌ Failed H2H w{week}: {e}")

    # Season top-5
    try:
        season_top_embeds = await build_season_top_embed_combined(league, int(week))
        embeds.append(season_top_embeds)
    except Exception as e:
        print(f"❌ Failed season top5 through w{week}: {e}")

    if embeds:
        await channel.send(embeds=embeds)
    else:
        await channel.send(f"🤷 No data available for week {week} yet.")


# ---------- Discord setup ----------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = False

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/New_York"))  # ET (DST aware)

# ---------- ESPN image constants ----------
PLAYER_IMG = "https://a.espncdn.com/combiner/i?img=/i/headshots/nfl/players/full/{player_id}.png&w=200&h=200"
TEAM_IMG = "https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"

# Map full team name → ESPN code (used for D/ST thumbnails)
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


# ---------- Utility: embed padding ----------
def pad_embeds(embed_list: list[discord.Embed]) -> None:
    """Make embed descriptions visually align by padding to the longest one."""
    if not embed_list:
        return
    max_len = 0
    for e in embed_list:
        desc = e.description or ""
        if len(desc) > max_len:
            max_len = len(desc)
    for e in embed_list:
        d = e.description or ""
        pad = max_len - len(d)
        if pad > 0:
            e.description = d + (" " * pad)  # simple visual padding


# ---------- ESPN helpers ----------
def build_league_from_settings(settings) -> League:
    return League(
        league_id=int(settings["league_id"]),
        year=int(settings["season"]),
        espn_s2=settings["espn_s2"],
        swid=settings["swid"]
    )


# ---------- Weekly & Season builders ----------
async def build_weekly_top_embeds(league: League, week: int, starters_only: bool = False) -> list[discord.Embed]:
    """Top player per desired position for a given week using box scores."""
    best: dict[str, dict | None] = {p: None for p in DESIRED_POSITIONS}
    week_boxes = league.box_scores(week=week)

    for game in week_boxes:
        for lineup, fteam in ((game.home_lineup, game.home_team), (game.away_lineup, game.away_team)):
            for bp in lineup:  # bp is a BoxScorePlayer
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


async def build_season_top_embed_combined(league, current_week: int, starters_only: bool = False) -> discord.Embed:
    desired = ['QB', 'RB', 'WR', 'TE', 'K', 'D/ST']
    season_points = {p: {} for p in desired}

    for wk in range(1, current_week + 1):
        for game in league.box_scores(week=wk):
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
                    if pos not in season_points:
                        continue
                    season_points[pos][bp.name] = season_points[pos].get(bp.name, 0.0) + float(pts)

    lines = []
    for pos in desired:
        top5 = sorted(season_points[pos].items(), key=lambda x: x[1], reverse=True)[:5]
        section = "\n".join(f"• **{name}** — {pts:.2f}" for name, pts in top5) if top5 else "_No data_"
        lines.append(f"**{pos}**\n{section}")

    return discord.Embed(
        title=f"Season Top 5 (through Week {current_week})",
        description="\n\n".join(lines),
        color=0xe67e22
    )



# ---------- Paginator (Week Navigator) ----------
class WeekNavigator(View):
    def __init__(self, week_embeds: list[list[discord.Embed]]):
        super().__init__(timeout=300)
        self.week_embeds = week_embeds  # list of lists (embeds per week page)
        self.index = len(week_embeds) - 1
        self.message: discord.Message | None = None

        # Select for quick week jump
        options = [discord.SelectOption(label=f"Week {i+1}", value=str(i)) for i in range(len(week_embeds))]
        self.select = Select(placeholder="Jump to week…", min_values=1, max_values=1, options=options)
        self.select.callback = self.jump_to_week
        self.add_item(self.select)

    def set_message(self, message: discord.Message):
        self.message = message

    def update_button_states(self):
        # FIX: toggle the button attributes, not children indices
        # These attributes are set by the @discord.ui.button decorators below
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


@bot.tree.command(name="weeklyrecap", description="Manually trigger a weekly recap")
async def weeklyrecap_slash(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        # Prefer configured channel; fallback to the channel where the command was used
        settings = await get_guild_settings(str(interaction.guild.id))
        channel = None
        if settings and settings.get("channel_id"):
            channel = interaction.guild.get_channel(int(settings["channel_id"]))
        if channel is None:
            channel = interaction.channel

        # Permission check (send + embeds)
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            await interaction.followup.send(f"❌ I can’t send messages in {channel.mention}. Give me **Send Messages**.", ephemeral=True)
            return
        if not perms.embed_links:
            await interaction.followup.send(f"❌ I can’t send embeds in {channel.mention}. Give me **Embed Links**.", ephemeral=True)
            return

        await send_weekly_recap_to_channel(interaction.guild, channel)
        await interaction.followup.send(f"✅ Weekly recap posted in {channel.mention}", ephemeral=True)

    except Exception as e:
        # Always tell you what went wrong instead of failing silently
        await interaction.followup.send(f"❌ Error while posting: `{e}`", ephemeral=True)


# ---------- Weekly recap core ----------
async def send_weekly_recap(ctx: commands.Context):
    guild_id = str(ctx.guild.id)
    settings = await get_guild_settings(guild_id)
    if not settings:
        await ctx.send("❌ This server hasn't been set up. Use `/setup` first.")
        return

    try:
        league = build_league_from_settings(settings)
    except Exception as e:
        await ctx.send(f"❌ Failed to initialize ESPN League: {e}")
        return

    # Decide target week: ESPN's current_week is commonly available as league.current_week
    try:
        week = int(league.current_week)
    except Exception:
        # Fallback
        week = 1

    embeds: list[discord.Embed] = []

    # === TOP PLAYERS PER POSITION (WEEKLY) ===
    try:
        weekly_top_embeds = await build_weekly_top_embeds(league, week)
        pad_embeds(weekly_top_embeds)
        embeds.extend(weekly_top_embeds)
    except Exception as e:
        print(f"❌ Failed to generate weekly top players for week {week}: {e}")

    # === HEAD-TO-HEAD MATCHUPS ===
    try:
        box_scores = league.box_scores(week=week)
        matchup_embed = Embed(
            title=f"Week {week} Head-to-Head Matchups",
            description="🏈 Weekly fantasy results",
            color=0xf39c12
        )
        for game in box_scores:
            home = game.home_team
            away = game.away_team
            home_score = game.home_score
            away_score = game.away_score

            home_valid = hasattr(home, "team_name")
            away_valid = hasattr(away, "team_name")

            if home_valid and away_valid:
                winner = home if home_score > away_score else away
                win_score = max(home_score, away_score)
                result = (
                    f"{home.team_name} ({home.wins}-{home.losses}) vs. {away.team_name} ({away.wins}-{away.losses})\n"
                    f"Score: {home_score:.1f} - {away_score:.1f}\n"
                    f"🏆 Winner: **{winner.team_name}** (**{win_score:.1f}**)"
                )
            elif home_valid:
                result = (
                    f"{home.team_name} ({home.wins}-{home.losses}) vs. BYE\n"
                    f"Score: {home_score:.1f} - 0.0\n"
                    f"🛌 **{home.team_name}** is on a bye week!"
                )
            elif away_valid:
                result = (
                    f"BYE vs. {away.team_name} ({away.wins}-{away.losses})\n"
                    f"Score: 0.0 - {away_score:.1f}\n"
                    f"🛌 **{away.team_name}** is on a bye week!"
                )
            else:
                continue

            matchup_embed.add_field(name="Matchup", value=result, inline=False)

        embeds.append(matchup_embed)
    except Exception as e:
        print(f"❌ Failed to fetch box scores for week {week}: {e}")

    # === TOP 5 PER POSITION (SEASON TOTALS THROUGH CURRENT WEEK) ===
    try:
        season_top_embeds = await build_season_top_embed_combined(league, week)
        embeds.append(season_top_embeds)
    except Exception as e:
        print(f"❌ Failed to generate top 5 players: {e}")

    # Paginated weeks (if you store per-week pages). For now, just send a single page with all embeds:
    await ctx.send(embeds=embeds)


# ---------- Scheduler (auto-post Tuesdays 11:00 AM ET) ----------
@scheduler.scheduled_job("cron", day_of_week="tue", hour=11, minute=0)
async def auto_post_weekly_recap():
    for guild in bot.guilds:
        try:
            settings = await get_guild_settings(str(guild.id))
            if not settings or not settings.get("autopost_enabled"):
                continue
            channel_id = settings.get("channel_id")
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            await send_weekly_recap_to_channel(guild, channel)
        except Exception as e:
            print(f"❌ Auto-post failed for guild {guild.id}: {e}")


# ---------- Entrypoint ----------
if __name__ == "__main__":
    token = get_discord_bot_token()
    asyncio.run(bot.start(token))
