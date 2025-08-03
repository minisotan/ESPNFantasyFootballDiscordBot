import discord
import json
import os
from discord.ext import commands
from discord.ui import Button, View
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from espn_api.football import League
from settings_manager import (
    get_guild_settings,
    set_guild_settings,
    get_discord_bot_token,
    load_settings,
    save_settings
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()

PLAYER_IMG = "https://a.espncdn.com/i/headshots/nfl/players/full/{player_id}.png"
TEAM_IMG = "https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"

SHOW_MATCHUPS = True
SHOW_WEEKLY_TOP = True
SHOW_TOP_5 = True
SHOW_RANKINGS = True

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
    await bot.tree.sync()
    scheduler.start()

@scheduler.scheduled_job("cron", day_of_week="tue", hour=15, minute=0)
async def auto_post_weekly_recap():
    for guild_id, config in load_settings().items():
        if not config.get("autopost_enabled"):
            continue

        guild = bot.get_guild(int(guild_id))
        if not guild:
            continue

        channel = guild.get_channel(config["channel_id"])
        if not channel:
            continue

        msg = await channel.send("⏳ Auto-posting weekly recap...")
        ctx = await bot.get_context(msg)
        await weeklyrecap(ctx)
        await msg.delete()
    print(f"✅ Logged in as {bot.user}")

@bot.tree.command(name="setup", description="Set up your ESPN Fantasy League for this server")
@app_commands.describe(
    league_id="Your ESPN league ID",
    season="The season year (e.g., 2024)",
    swid="Your SWID cookie (starts with { and ends with })",
    espn_s2="Your ESPN_S2 cookie"
)
async def setup(interaction: discord.Interaction, league_id: int, season: int, swid: str, espn_s2: str):
    guild_id = interaction.guild.id
    channel_id = interaction.channel.id
    set_guild_settings(guild_id, league_id, season, swid, espn_s2, channel_id)
    await interaction.response.send_message(f"✅ Setup complete! League ID: `{league_id}` Season: `{season}` Channel: <#{channel_id}>", ephemeral=True)


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
    current = get_guild_settings(guild_id)
    if not current:
        await interaction.response.send_message("❌ This server hasn't been set up yet. Use `/setup` first.", ephemeral=True)
        return

    updated = {
        "league_id": league_id or current["league_id"],
        "season": season or current["season"],
        "swid": swid or current["swid"],
        "espn_s2": espn_s2 or current["espn_s2"],
        "channel_id": channel.id if channel else current["channel_id"],
        "autopost_enabled": current.get("autopost_enabled", False)
    }

    settings = load_settings()
    settings[guild_id] = updated
    save_settings(settings)
    await interaction.response.send_message("✅ Settings updated successfully!", ephemeral=True)


@bot.tree.command(name="autopost", description="Enable or disable automatic weekly recaps")
@app_commands.describe(enabled="Set to true to enable, false to disable")
async def autopost(interaction: discord.Interaction, enabled: bool):
    guild_id = str(interaction.guild.id)
    settings = get_guild_settings(guild_id)
    if not settings:
        await interaction.response.send_message("❌ This server hasn't been set up. Use `/setup` first.", ephemeral=True)
        return

    settings["autopost_enabled"] = enabled
    all_settings = load_settings()
    all_settings[guild_id] = settings
    save_settings(all_settings)

    status = "✅ Auto-posting enabled! Recaps will post Tuesdays at 11:00 AM EST." if enabled else "❌ Auto-posting disabled."
    await interaction.response.send_message(status, ephemeral=True)


@bot.tree.command(name="weeklyrecap", description="Manually trigger a weekly recap")
async def weeklyrecap_slash(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Generating weekly recap...", ephemeral=False)
    msg = await interaction.original_response()
    ctx = await bot.get_context(msg)
    await weeklyrecap(ctx)
    await msg.delete()

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
        self.children[0].disabled = (self.index == 0)  # ⬅️
        self.children[1].disabled = (self.index == len(self.week_embeds) - 1)  # ➡️

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

async def weeklyrecap(ctx):
    guild_id = str(ctx.guild.id)
    settings = get_guild_settings(guild_id)

    if not settings:
        await ctx.send("❌ This server has not been set up. Use `/setup` to configure your league.")
        return

    if ctx.channel.id != settings["channel_id"]:
        await ctx.send("❌ This command can only be used in the configured channel.")
        return

    with open('weekly_data.json', 'r') as f:
        weekly_data = json.load(f)

    if not weekly_data:
        await ctx.send("❌ No weekly data found.")
        return

    with open('top_players.json', 'r') as f:
        top_players_data = json.load(f)

    with open('team_stats.json', 'r') as f:
        team_stats_data = json.load(f)

    league = League(
        league_id=settings["league_id"],
        year=settings["season"],
        swid=settings["swid"],
        espn_s2=settings["espn_s2"]
    )

    week_embeds = []

    for week_entry in weekly_data:
        week_label = week_entry['Week']
        embeds = []

        if SHOW_MATCHUPS:
            try:
                box_scores = league.box_scores(week=int(week_label.split()[-1]))
                embed = discord.Embed(
                    title=f"{week_label} Head-to-Head Matchups",
                    description="🏈 Weekly fantasy results",
                    color=0xf39c12
                )
                embed.set_footer(text=f"Week {len(week_embeds)+1} of {len(weekly_data)}")

                for game in box_scores:
                    try:
                        home = game.home_team
                        away = game.away_team
                        home_score = game.home_score
                        away_score = game.away_score

                        if not hasattr(home, 'team_name') or not hasattr(away, 'team_name'):
                            continue

                        winner = home if home_score > away_score else away
                        win_score = max(home_score, away_score)

                        result = (
                            f"{home.team_name} ({home.wins}-{home.losses}) vs. {away.team_name} ({away.wins}-{away.losses})\n"
                            f"Score: {home_score} - {away_score}\n"
                            f"🏆 Winner: **{winner.team_name}** (**{win_score}**)"
                        )
                        embed.add_field(name="", value=result, inline=False)
                    except Exception as e:
                        print(f"Skipping bad box score in {week_label}: {e}")

                embeds.append(embed)
            except Exception as e:
                print(f"Failed to get box scores: {e}")

        if SHOW_WEEKLY_TOP:
            for pos in ['QB', 'RB', 'WR', 'TE', 'D/ST', 'K']:
                name = week_entry.get(f"{pos} Player", "")
                points = week_entry.get(f"{pos} Points", "")
                player_id = week_entry.get(f"{pos} ID", "")

                image_url = None
                if pos == 'D/ST' and name:
                    code = TEAM_LOGO.get(name.replace(" D/ST", "").strip())
                    if code:
                        image_url = TEAM_IMG.format(code=code)
                elif player_id:
                    image_url = PLAYER_IMG.format(player_id=player_id)

                embed = discord.Embed(
                    title=f"{week_label} Top {pos}",
                    description=f"**{name}**\nFantasy Points: **{points}**",
                    color=0x1abc9c
                )
                if image_url:
                    embed.set_thumbnail(url=image_url)

                embeds.append(embed)

        if SHOW_TOP_5:
            embed = discord.Embed(
                title="🏅 Season Top 5 by Position",
                description="Here are the top 5 performers at each position.",
                color=0x9b59b6
            )
            for pos in ['QB', 'RB', 'WR', 'TE', 'D/ST', 'K']:
                top_players = [p for p in top_players_data if p['Position'] == pos][:5]
                field_value = "\n".join(
                    f"{i + 1}. {p['Player']} — {p['Total Points']} pts (Avg: {p['Average Points']})"
                    for i, p in enumerate(top_players)
                )
                embed.add_field(name=f"Top 5 {pos}s", value=field_value, inline=False)
            embeds.append(embed)

        if SHOW_RANKINGS:
            embed = discord.Embed(
                title="📊 Power Rankings",
                description="Sorted by Wins, then Points",
                color=0x2980b9
            )
            for i, team in enumerate(team_stats_data, 1):
                embed.add_field(
                    name=f"{i}. {team['Team Name'].strip()}",
                    value=f"Record: {team['Wins']}-{team['Losses']} | Total Pts: {team['Total Points']}",
                    inline=False
                )
            embeds.append(embed)

        week_embeds.append(embeds)

    latest_index = len(week_embeds) - 1
    view = WeekNavigator(week_embeds)
    msg = await ctx.send(embeds=week_embeds[latest_index], view=view)
    view.set_message(msg)

bot.run(get_discord_bot_token())
