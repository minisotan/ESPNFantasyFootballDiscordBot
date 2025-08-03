import json
import os

from dotenv import load_dotenv
load_dotenv()

SETTINGS_FILE = 'settings.json'

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({}, f)
    with open(SETTINGS_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}  # fallback if file is empty or corrupted

def save_settings(data):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def get_guild_settings(guild_id):
    data = load_settings()
    return data.get(str(guild_id), None)

def get_schedule(guild_id):
    settings = get_guild_settings(guild_id)
    return settings.get("schedule_day", "tue"), settings.get("schedule_time", "10:00")

def set_guild_settings(guild_id, league_id, season, swid, espn_s2, channel_id):
    data = load_settings()
    data[str(guild_id)] = {
        "league_id": league_id,
        "season": season,
        "swid": swid,
        "espn_s2": espn_s2,
        "channel_id": channel_id,
        "autopost_enabled": False  # Add this so it always exists

    }
    save_settings(data)

def get_discord_bot_token():
    token = os.environ.get("DISCORD_TOKEN")
    print(f"DISCORD_TOKEN FOUND: {bool(token)}")
    return token
