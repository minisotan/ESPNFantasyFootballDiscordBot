import discord
from discord.ext import commands
from discord.ui import Button, View
from discord import app_commands
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from espn_api.football import League
from settings_manager import (
    get_guild_settings,
    set_guild_settings,
    set_autopost,
    get_discord_bot_token,
    init_db
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/New_York"))

PLAYER_IMG = "https://a.espncdn.com/i/headshots/nfl/players/full/{player_id}.png"
TEAM_IMG = "https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"

TEAM_LOGO = {
    "49ers": "sf", "Bears": "chi", "Bengals": "cin", "Bills": "buf", "Broncos": "den", "Browns": "cle",
    "Buccaneers": "tb", "Cardinals": "ari", "Chargers": "lac", "Chiefs": "kc", "Colts": "ind", "Commanders": "wsh",
    "Cowboys": "dal", "Dolphins": "mia", "Eagles": "phi", "Falcons": "atl", "Giants": "nyg", "Jaguars": "jax",
    "Jets": "nyj", "Lions": "det", "Packers": "gb", "Panthers": "car", "Patriots": "ne", "Raiders": "lv",
    "Rams": "lar", "Ravens": "bal", "Saints": "no", "Seahawks": "sea", "Steelers": "pit", "Texans": "hou",
    "Titans": "ten", "Vikings": "min"
}

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    scheduler.start()
    print(f"✅ Logged in as {bot.user}")

@scheduler.scheduled_job("cron", day_of_week="tue", hour=11, minute=0)
async def auto_post_weekly_recap():
    for guild in bot.guilds:
        settings = await get_guild_settings(str(guild.id))
        if not settings or not settings.get("autopost_enabled"):
            continue
        channel = guild.get_channel(settings["channel_id"])
        if channel:
            msg = await channel.send("⏳ Auto-posting weekly recap...")
            ctx = await bot.get_context(msg)
            await weeklyrecap(ctx)
            await msg.delete()

@bot.tree.command(name="setup", description="Set up your ESPN Fantasy League for this server")
@app_commands.describe(
    league_id="Your ESPN league ID",
    season="The season year (e.g., 2024)",
    swid="Your SWID cookie (starts with { and ends with })",
    espn_s2="Your ESPN_S2 cookie"
)
async def setup(interaction: discord.Interaction, league_id: int, season: int, swid: str, espn_s2: str):
    await set_guild_settings(interaction.guild.id, league_id, season, swid, espn_s2, interaction.channel.id)
    await interaction.response.send_message(f"✅ Setup complete! League ID: `{league_id}` Season: `{season}`", ephemeral=True)

@bot.tree.command(name="configure", description="Update ESPN league info or change the bot’s posting channel")
@app_commands.describe(
    league_id="(Optional) Your ESPN league ID",
    season="(Optional) The season year",
    swid="(Optional) Your SWID cookie",
    espn_s2="(Optional) Your ESPN_S2 cookie",
    channel="(Optional) Set a different channel for weekly recaps"
)
async def configure(
    interaction: discord.Interaction,
    league_id: int = None,
    season: int = None,
    swid: str = None,
    espn_s2: str = None,
    channel: discord.TextChannel = None
):
    guild_id = str(interaction.guild.id)
    current = await get_guild_settings(guild_id)
    if not current:
        await interaction.response.send_message("❌ This server hasn't been set up yet. Use `/setup` first.", ephemeral=True)
        return

    updated = {
        "league_id": league_id or current["league_id"],
        "season": season or current["season"],
        "swid": swid or current["swid"],
        "espn_s2": espn_s2 or current["espn_s2"],
        "channel_id": channel.id if channel else current["channel_id"]
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
        "✅ Auto-posting enabled. Weekly recaps will be posted every Tuesday at 11 AM ET."
        if enabled else
       "❌ Auto-posting disabled."
        )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="weeklyrecap", description="Manually trigger a weekly recap")
async def weeklyrecap_slash(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Generating weekly recap...", ephemeral=False)
    msg = await interaction.original_response()
    ctx = await bot.get_context(msg)
    await weeklyrecap(ctx)
    await msg.delete()

# === Weekly Recap Logic ===
async def weeklyrecap(ctx):
    from discord import Embed
    guild_id = str(ctx.guild.id)
    settings = await get_guild_settings(guild_id)

    if not settings:
        await ctx.send("❌ This server has not been set up. Use `/setup` to configure your league.")
        return
    if ctx.channel.id != settings["channel_id"]:
        await ctx.send("❌ This command can only be used in the configured channel.")
        return

    try:
        league = League(
            league_id=settings["league_id"],
            year=settings["season"],
            swid=settings["swid"],
            espn_s2=settings["espn_s2"]
        )
    except Exception as e:
        await ctx.send(f"❌ Failed to connect to ESPN: {e}")
        return

    current_week = league.current_week
    week_embeds = []

    for week in range(1, current_week + 1):
        embeds = []

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
                    continue  # Both teams invalid? Skip.

                matchup_embed.add_field(name="Matchup", value=result, inline=False)
            embeds.append(matchup_embed)
        except Exception as e:
            print(f"❌ Failed to fetch box scores for week {week}: {e}")

  # === TOP PLAYER PER POSITION THIS WEEK ===
        try:
            desired_positions = ['QB', 'RB', 'WR', 'TE', 'K', 'D/ST']
            best = {p: None for p in desired_positions}

            # Use box scores for the exact week's points and team mapping.
            week_boxes = league.box_scores(week=week)

            for game in week_boxes:
                # Pair each lineup with its fantasy team
                for lineup, fteam in ((game.home_lineup, game.home_team), (game.away_lineup, game.away_team)):
                    for bp in lineup:
                        # bp: BoxScorePlayer
                        pts = getattr(bp, "points", None)
                        base_pos = getattr(bp, "position", None)  # True NFL position (e.g., 'RB')
                        slot_pos = getattr(bp, "slot_position", None)  # Roster slot (e.g., 'FLEX', 'BE')

                        if pts is None or base_pos is None:
                            continue

                        # Normalize D/ST names/position
                        pos = base_pos
                        if pos in ("DST", "DEF", "Def"):
                            pos = "D/ST"

                        # If the pos isn't directly one of our targets, try to resolve (e.g., FLEX -> base_pos)
                        if pos not in desired_positions:
                            if base_pos in desired_positions:
                                pos = base_pos
                            else:
                                continue

                        current = best.get(pos)
                        if current is None or float(pts) > current["points"]:
                            best[pos] = {
                                "name": bp.name,
                                "points": float(pts),
                                "id": getattr(bp, "playerId", None),
                                "team": getattr(fteam, "team_name", "Unknown")
                            }

            # Emit embeds for each position
            for pos in desired_positions:
                top = best.get(pos)
                if not top:
                    continue

                # Build image URL
                if pos == "D/ST":
                    # Name like "Patriots D/ST" -> "Patriots"
                    team_key = top["name"].replace(" D/ST", "").strip()
                    code = TEAM_LOGO.get(team_key)
                    image_url = TEAM_IMG.format(code=code) if code else None
                else:
                    image_url = PLAYER_IMG.format(player_id=top["id"]) if top["id"] else None

                embed = discord.Embed(
                    title=f"Week {week} Top {pos}",
                    description=(
                        f"**{top['name']}** ({pos})\n"
                        f"Fantasy Points: **{top['points']:.2f}**\n"
                        f"Fantasy Team: *{top['team']}*"
                    ),
                    color=0x1abc9c
                )
                if image_url:
                    embed.set_thumbnail(url=image_url)
                embeds.append(embed)

        except Exception as e:
            print(f"❌ Failed to generate top players for week {week}: {e}")

        # === POWER RANKINGS ===
        try:
            sorted_teams = sorted(league.teams, key=lambda t: (-t.wins, -t.points_for))
            power_embed = Embed(
                title="📊 Power Rankings",
                description="Sorted by Wins, then Points",
                color=0x2980b9
            )
            for i, team in enumerate(sorted_teams, 1):
                power_embed.add_field(
                    name=f"{i}. {team.team_name.strip()}",
                    value=f"Record: {team.wins}-{team.losses} | Total Pts: {team.points_for:.1f}",
                    inline=False
                )
            embeds.append(power_embed)
        except Exception as e:
            print(f"❌ Failed to generate power rankings: {e}")

        # ✅ FIXED: Ensure this always runs
        week_embeds.append(embeds)

    if not week_embeds:
        await ctx.send("❌ No data available. Please check your league setup or ESPN cookies.")
        return

    # === PAGINATION VIEW ===
    class WeekNavigator(View):
        def __init__(self, week_embeds):
            super().__init__(timeout=None)
            self.week_embeds = week_embeds
            self.index = len(week_embeds) - 1
            self.message = None

            self.select = discord.ui.Select(placeholder="Select a week")
            for i in range(len(week_embeds)):
                self.select.add_option(label=f"Week {i + 1}", value=str(i))
            self.select.callback = self.jump_to_week
            self.add_item(self.select)

        def set_message(self, msg):
            self.message = msg
            self.update_button_states()

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

    latest_index = len(week_embeds) - 1
    view = WeekNavigator(week_embeds)
    msg = await ctx.send(embeds=week_embeds[latest_index], view=view)
    view.set_message(msg)

if __name__ == "__main__":
    import asyncio
    token = get_discord_bot_token()
    asyncio.run(bot.start(token))
