import json
import aiosqlite
import os
import re
from datetime import datetime, timezone, timedelta
from config import DB_PATH, DEFAULT_CHANNELS, DEFAULT_KEYWORDS, OWNER_ID

from reward_caps import extract_reward_cap

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitoring_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('channel', 'keyword')),
  value TEXT NOT NULL,
  normalized TEXT NOT NULL,
  UNIQUE(kind, normalized)
);
CREATE TABLE IF NOT EXISTS admins (
  user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
  appointed_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE NOT NULL,
  title TEXT,
  custom_title TEXT,
  reward_cap REAL,
  reward_cap_token TEXT,
  start_ts INTEGER,
  end_ts INTEGER,
  pairs TEXT DEFAULT '[]',
  rules TEXT DEFAULT '',
  min_volume REAL,
  total_pool REAL,
  remaining_pool REAL,
  pool_token TEXT DEFAULT 'BNB',
  fixed_slots TEXT DEFAULT '[]',
  leaderboard_url TEXT,
  settings_version INTEGER NOT NULL DEFAULT 0,
  stats_version INTEGER,
  last_stats TEXT,
  last_scraped INTEGER,
  created_at INTEGER NOT NULL
);
"""


class DB:
    def __init__(self):
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        async with self.conn.execute("PRAGMA table_info(events)") as c:
            columns = {r["name"] for r in await c.fetchall()}
        if "custom_title" not in columns:
            await self.conn.execute("ALTER TABLE events ADD COLUMN custom_title TEXT")
        for name, sql_type in (("reward_cap", "REAL"), ("reward_cap_token", "TEXT"),
                               ("settings_version", "INTEGER NOT NULL DEFAULT 0"), ("stats_version", "INTEGER")):
            if name not in columns:
                await self.conn.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type}")
        if await self.get_setting("monitoring_items_initialized") is None:
            old_channel = await self.get_setting("channel")
            channels = [old_channel] if old_channel is not None else DEFAULT_CHANNELS
            old_keywords = await self.get_setting("keywords")
            keywords = json.loads(old_keywords) if old_keywords is not None else DEFAULT_KEYWORDS
            for kind, values in (("channel", channels), ("keyword", keywords)):
                for value in values:
                    if not value.strip():
                        continue
                    try:
                        value = self.normalize_channel(value) if kind == "channel" else value.strip()
                    except ValueError:
                        continue
                    await self.conn.execute("INSERT OR IGNORE INTO monitoring_items(kind,value,normalized) VALUES(?,?,?)",
                                            (kind, value, value.casefold()))
            await self.conn.execute("INSERT INTO settings(key,value) VALUES('monitoring_items_initialized','1')")
        async with self.conn.execute("SELECT id,rules,pool_token FROM events WHERE reward_cap IS NULL") as cur:
            legacy = await cur.fetchall()
        for row in legacy:
            cap, token = extract_reward_cap(row["rules"], row["pool_token"] or "BNB")
            if cap is not None:
                await self.conn.execute("UPDATE events SET reward_cap=?,reward_cap_token=? WHERE id=?",
                                        (cap, token, row["id"]))
        await self.conn.commit()

    # settings
    async def get_setting(self, key):
        async with self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r["value"] if r else None

    async def set_setting(self, key, value):
        await self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        await self.conn.commit()

    @staticmethod
    def normalize_channel(value: str) -> str:
        value = value.strip()
        value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I).rstrip("/").lstrip("@")
        if re.fullmatch(r"-100[1-9]\d*", value):
            return value
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
            raise ValueError("Send a channel @username, t.me/username URL, or -100… channel ID.")
        return value.lower()

    async def monitoring_items(self, kind):
        async with self.conn.execute("SELECT id,value FROM monitoring_items WHERE kind=? ORDER BY id", (kind,)) as c:
            return [dict(r) for r in await c.fetchall()]

    async def channels(self) -> list[str]:
        return [r["value"] for r in await self.monitoring_items("channel")]

    async def keywords(self) -> list[str]:
        return [r["value"] for r in await self.monitoring_items("keyword")]

    async def add_monitoring_item(self, actor_id, kind, value):
        if not await self.is_admin(actor_id):
            raise PermissionError("Admins only.")
        if kind not in {"channel", "keyword"}:
            raise ValueError("Unknown setting.")
        value = self.normalize_channel(value) if kind == "channel" else value.strip()
        if not value or len(value) > 100:
            raise ValueError("Use 1–100 characters.")
        cur = await self.conn.execute("""INSERT OR IGNORE INTO monitoring_items(kind,value,normalized)
            SELECT ?,?,? WHERE ?=? OR EXISTS(SELECT 1 FROM admins WHERE user_id=?)""",
            (kind, value, value.casefold(), actor_id, OWNER_ID, actor_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_monitoring_item(self, actor_id, kind, item_id):
        if not await self.is_admin(actor_id):
            raise PermissionError("Admins only.")
        cur = await self.conn.execute("""DELETE FROM monitoring_items WHERE id=? AND kind=?
            AND (?=? OR EXISTS(SELECT 1 FROM admins WHERE user_id=?))""",
            (item_id, kind, actor_id, OWNER_ID, actor_id))
        await self.conn.commit()
        return cur.rowcount > 0

    # Roles are separate from event/profile data. The owner is configuration-only.
    def require_owner(self, actor_id: int):
        if actor_id != OWNER_ID:
            raise PermissionError("Only the bot owner can manage admins.")

    async def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        async with self.conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as c:
            return await c.fetchone() is not None

    async def list_admins(self) -> list[int]:
        async with self.conn.execute("SELECT user_id FROM admins ORDER BY user_id") as c:
            return [r["user_id"] for r in await c.fetchall() if r["user_id"] != OWNER_ID]

    async def notification_ids(self) -> list[int]:
        return [OWNER_ID, *await self.list_admins()]

    async def add_admin(self, actor_id: int, user_id: int) -> bool:
        self.require_owner(actor_id)
        if user_id <= 0 or user_id == OWNER_ID:
            raise ValueError("Send a positive Telegram user ID other than the owner's ID.")
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO admins(user_id, appointed_by, created_at) VALUES(?,?,?)",
            (user_id, actor_id, int(datetime.now(timezone.utc).timestamp())))
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_admin(self, actor_id: int, user_id: int) -> bool:
        self.require_owner(actor_id)
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be removed through the bot.")
        cur = await self.conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _event(row):
        event = dict(row)
        event["title"] = event.get("custom_title") or event.get("title")
        event["stats_stale"] = bool(event.get("last_stats") and
                                    event.get("stats_version") != event.get("settings_version", 0))
        return event

    async def set_event_title(self, actor_id: int, eid: int, title: str):
        title = title.strip()
        if not title or len(title) > 200:
            raise ValueError("The title must contain 1–200 characters.")
        if not await self.is_admin(actor_id):
            raise PermissionError("Admins only.")
        now = int(datetime.now(timezone.utc).timestamp())
        cur = await self.conn.execute("""
            UPDATE events SET custom_title=? WHERE id=?
            AND (start_ts IS NULL OR start_ts <= ?)
            AND (end_ts IS NULL OR end_ts > ?)
            AND (? = ? OR EXISTS (SELECT 1 FROM admins WHERE user_id=?))
        """, (title, eid, now, now, actor_id, OWNER_ID, actor_id))
        await self.conn.commit()
        if cur.rowcount != 1:
            raise ValueError("Event is missing, no longer live, or your access was revoked.")

    # events
    async def upsert_event(self, ev: dict) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        await self.conn.execute("""
          INSERT INTO events(url,title,start_ts,end_ts,pairs,rules,min_volume,total_pool,remaining_pool,
                             pool_token,fixed_slots,leaderboard_url,reward_cap,reward_cap_token,created_at)
          VALUES(:url,:title,:start_ts,:end_ts,:pairs,:rules,:min_volume,:total_pool,:remaining_pool,
                 :pool_token,:fixed_slots,:leaderboard_url,:reward_cap,:reward_cap_token,:now)
          ON CONFLICT(url) DO UPDATE SET title=excluded.title,start_ts=excluded.start_ts,end_ts=excluded.end_ts,
            pairs=excluded.pairs,rules=excluded.rules,total_pool=COALESCE(excluded.total_pool, events.total_pool),
            remaining_pool=COALESCE(excluded.remaining_pool, events.remaining_pool),pool_token=excluded.pool_token,
            fixed_slots=CASE WHEN excluded.fixed_slots IN ('[]','') THEN events.fixed_slots ELSE excluded.fixed_slots END,
            min_volume=COALESCE(excluded.min_volume, events.min_volume),
            leaderboard_url=COALESCE(excluded.leaderboard_url, events.leaderboard_url),
            reward_cap=excluded.reward_cap,reward_cap_token=excluded.reward_cap_token,
            settings_version=events.settings_version+1
        """, {"reward_cap": None, "reward_cap_token": None, **ev, "pairs": json.dumps(ev.get("pairs", [])),
              "fixed_slots": json.dumps(ev.get("fixed_slots", [])), "now": now})
        await self.conn.commit()
        async with self.conn.execute("SELECT id FROM events WHERE url=?", (ev["url"],)) as c:
            return (await c.fetchone())["id"]

    async def list_events(self, live_only=False):
        q = "SELECT * FROM events"
        args = ()
        if live_only:
            q += " WHERE end_ts IS NULL OR end_ts > ?"
            args = (int(datetime.now(timezone.utc).timestamp()),)
        q += " ORDER BY COALESCE(end_ts, 9e18) ASC"
        async with self.conn.execute(q, args) as c:
            return [self._event(r) for r in await c.fetchall()]

    async def get_event(self, eid: int):
        async with self.conn.execute("SELECT * FROM events WHERE id=?", (eid,)) as c:
            r = await c.fetchone()
            return self._event(r) if r else None

    async def update_stats(self, eid: int, stats: dict, expected_version=None):
        cur = await self.conn.execute("""UPDATE events SET last_stats=?, last_scraped=?,
            stats_version=settings_version WHERE id=?
            AND (? IS NULL OR settings_version=?)""",
            (json.dumps(stats), int(datetime.now(timezone.utc).timestamp()), eid,
             expected_version, expected_version))
        await self.conn.commit()
        return cur.rowcount == 1

    async def set_field(self, eid: int, field: str, value):
        allowed = {"leaderboard_url", "remaining_pool", "total_pool", "min_volume"}
        if field not in allowed:
            raise ValueError("field not allowed")
        await self.conn.execute(f"UPDATE events SET {field}=?, settings_version=settings_version+1 WHERE id=?", (value, eid))
        await self.conn.commit()

    async def delete_event(self, eid: int):
        await self.conn.execute("DELETE FROM events WHERE id=?", (eid,))
        await self.conn.commit()

    async def purge_old(self, days: int) -> int:
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        cur = await self.conn.execute("DELETE FROM events WHERE end_ts IS NOT NULL AND end_ts < ?", (cutoff,))
        await self.conn.commit()
        return cur.rowcount

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None


db = DB()
