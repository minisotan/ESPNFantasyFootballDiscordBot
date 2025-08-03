import json
import os
import subprocess
import pandas as pd
from collections import defaultdict
from espn_api.football import League
from config import LEAGUE_ID, SEASON, SWID, ESPN_S2

# Define file paths
WEEKLY_JSON = 'weekly_data.json'
TEAM_STATS_JSON = 'team_stats.json'
TOP_PLAYERS_JSON = 'top_players.json'

# Initialize the league
league = League(league_id=LEAGUE_ID, year=SEASON, swid=SWID, espn_s2=ESPN_S2)
positions = ['QB', 'RB', 'WR', 'TE', 'D/ST', 'K']

### Section 1: Team Stats
teams_data = []

for team in league.teams:
    total_points = sum(team.scores)
    games_played = sum(1 for score in team.scores if score > 0)
    average_points = round(total_points / games_played, 1) if games_played > 0 else 0

    teams_data.append({
        'Team Name': team.team_name,
        'Wins': team.wins,
        'Losses': team.losses,
        'Total Points': round(total_points, 1),
        'Average Points': average_points
    })

df_teams = pd.DataFrame(teams_data)
df_teams = df_teams.sort_values(by=['Wins', 'Total Points'], ascending=False).reset_index(drop=True)

### Section 2: Top 5 Players by Position
top_players_data = []

for pos in positions:
    all_players = []

    for team in league.teams:
        for player in team.roster:
            if player.position == pos:
                total_points = player.total_points
                games_played = sum(1 for score in team.scores if score > 0)
                avg_points = round(total_points / games_played, 1) if games_played > 0 else 0

                all_players.append({
                    'Player': player.name,
                    'Total Points': total_points,
                    'Average Points': avg_points,
                    'Position': pos
                })

    top_players = sorted(all_players, key=lambda x: x['Total Points'], reverse=True)[:10]
    top_players_data.extend(top_players)

df_top_players = pd.DataFrame(top_players_data)

### Section 3: Weekly Top Performers
if os.path.exists(WEEKLY_JSON):
    with open(WEEKLY_JSON, 'r') as f:
        try:
            weekly_data = json.load(f)
        except json.JSONDecodeError:
            print(f"{WEEKLY_JSON} is empty or corrupted. Initializing as empty list.")
            weekly_data = []
else:
    weekly_data = []

existing_weeks = {entry['Week'] for entry in weekly_data}

# Export through all 18 regular season weeks
weeks_played = 18
print(f"Forcing export through Week 18")

for week_num in range(1, weeks_played + 1):
    week_label = f'Week {week_num}'
    if week_label in existing_weeks:
        continue

    print(f"Processing data for {week_label}")
    week_data = {'Week': week_label}
    best_by_position = {}

    try:
        box_scores = league.box_scores(week=week_num)
    except Exception as e:
        print(f"  Skipping week {week_num}: {e}")
        continue

    for match in box_scores:
        for team in [match.home_lineup, match.away_lineup]:
            for player in team:
                pos = player.position
                if pos not in positions:
                    continue

                pts = player.points
                if pos not in best_by_position or pts > best_by_position[pos].points:
                    best_by_position[pos] = player  # store full player object

    for pos in positions:
        best_player = best_by_position.get(pos)
        if best_player:
            week_data[f"{pos} Player"] = best_player.name
            week_data[f"{pos} Points"] = round(best_player.points, 1)
            week_data[f"{pos} ID"] = str(best_player.playerId)
        else:
            week_data[f"{pos} Player"] = ""
            week_data[f"{pos} Points"] = ""
            week_data[f"{pos} ID"] = ""

    weekly_data.append(week_data)

# Save updated data to JSON
with open(WEEKLY_JSON, 'w') as f:
    json.dump(weekly_data, f, indent=4)

with open(TEAM_STATS_JSON, 'w') as f:
    json.dump(df_teams.to_dict(orient='records'), f, indent=4)

with open(TOP_PLAYERS_JSON, 'w') as f:
    json.dump(df_top_players.to_dict(orient='records'), f, indent=4)

print("Fantasy Football data exported successfully to JSON.")

# Auto-run Discord bot after export
subprocess.run(["python", "bot.py"])