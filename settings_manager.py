import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "settings.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                league_id TEXT,
                season TEXT,
                swid TEXT,
                espn_s2 TEXT,
                channel_id TEXT,
                autopost_enabled INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def set_guild_settings(guild_id, league_id, season, swid, espn_s2, channel_id):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO guild_settings (
                guild_id, league_id, season, swid, espn_s2, channel_id, autopost_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                (SELECT autopost_enabled FROM guild_settings WHERE guild_id = ?), 0))
        """, (str(guild_id), league_id, season, swid, espn_s2, channel_id, str(guild_id)))
        await db.commit()

async def get_guild_settings(guild_id):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT league_id, season, swid, espn_s2, channel_id, autopost_enabled FROM guild_settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "league_id": int(row[0]),
                    "season": int(row[1]),
                    "swid": row[2],
                    "espn_s2": row[3],
                    "channel_id": int(row[4]),
                    "autopost_enabled": bool(row[5])
                }
            return None

async def set_autopost(guild_id, enabled):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE guild_settings SET autopost_enabled = ? WHERE guild_id = ?", (int(enabled), str(guild_id)))
        await db.commit()

async def get_discord_bot_token():
    return os.environ.get("DISCORD_TOKEN")
