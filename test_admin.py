"""Offline permission and Telegram-handler regression tests, no network required."""
import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ["BOT_TOKEN"] = "123456:dummy-test-token"
os.environ["OWNER_ID"] = "111"
os.environ["ADMIN_IDS"] = ""

import pytest
import db as database
import handlers


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(database, "OWNER_ID", 111)
    monkeypatch.setattr(handlers, "OWNER_ID", 111)
    db = database.DB()
    run(db.init())
    monkeypatch.setattr(handlers, "db", db)
    yield db
    run(db.close())


def event(**changes):
    return dict(url="https://www.binance.com/test", title="Scraped title",
                start_ts=None, end_ts=None, pairs=[], rules="rules", min_volume=100,
                total_pool=80, remaining_pool=40, pool_token="BNB", fixed_slots=[],
                leaderboard_url=None, **changes)


def update(user_id, data=None, text=""):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    chat = SimpleNamespace(send_message=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg) if data else None
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_chat=chat, callback_query=query, message=msg)


def test_owner_can_appoint_admin(store):
    assert run(store.add_admin(111, 222))
    assert run(store.is_admin(222))


def test_owner_can_revoke_admin(store):
    run(store.add_admin(111, 222))
    assert run(store.remove_admin(111, 222))
    assert not run(store.is_admin(222))


def test_regular_admin_cannot_appoint(store):
    run(store.add_admin(111, 222))
    with pytest.raises(PermissionError):
        run(store.add_admin(222, 333))
    assert not run(store.is_admin(333))


def test_regular_admin_cannot_revoke(store):
    run(store.add_admin(111, 222))
    run(store.add_admin(111, 333))
    with pytest.raises(PermissionError):
        run(store.remove_admin(222, 333))
    assert run(store.is_admin(333))


def test_owner_cannot_be_revoked(store):
    with pytest.raises(ValueError):
        run(store.remove_admin(111, 111))
    assert run(store.is_admin(111))


def test_forged_owner_callbacks_are_denied(store):
    run(store.add_admin(111, 222))
    ctx = SimpleNamespace(user_data={})
    for data in ["o:admins", "o:add", "o:remove:222", "o:confirm:222"]:
        run(handlers.on_callback(update(222, data), ctx))
        assert ctx.user_data == {}
        assert run(store.list_admins()) == [222]


def test_forged_owner_text_prompt_is_denied(store):
    run(store.add_admin(111, 222))
    run(handlers.on_text(update(222, text="333"), SimpleNamespace(user_data={"await": ("admin_add",)})))
    assert not run(store.is_admin(333))


def test_owner_management_handler_flow(store):
    async def flow():
        ctx = SimpleNamespace(user_data={})
        await handlers.admin(update(111), ctx)
        await handlers.on_callback(update(111, "o:admins"), ctx)
        await handlers.on_callback(update(111, "o:add"), ctx)
        await handlers.on_text(update(111, text="222"), ctx)
        assert await store.list_admins() == [222]
        await handlers.on_callback(update(111, "o:remove:222"), ctx)
        assert await store.is_admin(222)  # confirmation required
        await handlers.on_callback(update(111, "o:confirm:222"), ctx)
        assert not await store.is_admin(222)
    run(flow())


@pytest.mark.parametrize("actor", [111, 222])
def test_owner_and_admin_live_title_handler_flow(store, actor):
    async def flow():
        await store.add_admin(111, 222)
        eid = await store.upsert_event(event())
        before = await store.get_event(eid)
        ctx = SimpleNamespace(user_data={})
        await handlers.admin(update(actor), ctx)
        await handlers.on_callback(update(actor, "a:list"), ctx)
        await handlers.on_callback(update(actor, f"ev:{eid}"), ctx)
        await handlers.on_callback(update(actor, f"aedit:{eid}"), ctx)
        await handlers.on_callback(update(actor, f"ef:{eid}:title"), ctx)
        await handlers.on_text(update(actor, text="My <Live> & Event"), ctx)
        assert (await store.list_events(live_only=True))[0]["title"] == "My <Live> & Event"
        viewed = update(actor, f"ev:{eid}")
        await handlers.on_callback(viewed, ctx)
        assert "My &lt;Live&gt; &amp; Event" in viewed.callback_query.edit_message_text.call_args.args[0]
        after = await store.get_event(eid)
        for field in ["url", "start_ts", "end_ts", "total_pool", "remaining_pool", "leaderboard_url"]:
            assert after[field] == before[field]
    run(flow())


def test_revoked_admin_pending_title_is_denied(store):
    run(store.add_admin(111, 222))
    eid = run(store.upsert_event(event()))
    ctx = SimpleNamespace(user_data={"await": ("field", eid, "title")})
    run(store.remove_admin(111, 222))
    run(handlers.on_text(update(222, text="Unauthorized"), ctx))
    assert run(store.get_event(eid))["title"] == "Scraped title"


def test_ordinary_user_cannot_edit_title(store):
    eid = run(store.upsert_event(event()))
    with pytest.raises(PermissionError):
        run(store.set_event_title(333, eid, "Unauthorized"))
    assert run(store.get_event(eid))["title"] == "Scraped title"


def test_custom_title_survives_reingestion_and_restart(store):
    eid = run(store.upsert_event(event()))
    run(store.set_event_title(111, eid, "Custom title"))
    run(store.upsert_event(event()))
    run(store.close())
    run(store.init())
    assert run(store.get_event(eid))["title"] == "Custom title"


def test_admin_ids_environment_never_grants_access(tmp_path):
    env = {**os.environ, "BOT_TOKEN": "123456:dummy-test-token", "OWNER_ID": "111",
           "ADMIN_IDS": "222,333", "DB_PATH": str(tmp_path / "fresh.db")}
    result = subprocess.run([sys.executable, "-c", '''
import asyncio
import config
from db import DB
async def check():
    store = DB()
    await store.init()
    try:
        assert not hasattr(config, "ADMIN_IDS")
        assert await store.is_admin(111)
        assert await store.list_admins() == []
        assert not await store.is_admin(222)
        assert not await store.is_admin(333)
    finally:
        await store.close()
asyncio.run(check())
'''], env=env, cwd=Path(__file__).parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_interactive_appointment_and_revocation_survive_restart(store, monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "222,333")
    async def flow():
        ctx = SimpleNamespace(user_data={})
        await handlers.admin(update(111), ctx)
        await handlers.on_callback(update(111, "o:admins"), ctx)
        await handlers.on_callback(update(111, "o:add"), ctx)
        await handlers.on_text(update(111, text="222"), ctx)
        await store.close()
        await store.init()
        assert await store.list_admins() == [222]
        await handlers.on_callback(update(111, "o:remove:222"), ctx)
        await handlers.on_callback(update(111, "o:confirm:222"), ctx)
        await store.close()
        await store.init()
        assert await store.list_admins() == []
        assert not await store.is_admin(222)
        assert not await store.is_admin(333)
    run(flow())


@pytest.mark.parametrize("start,end", [(None, 1), (9999999999, None)])
def test_closed_and_upcoming_titles_cannot_change(store, start, end):
    ev = event()
    ev.update(start_ts=start, end_ts=end)
    eid = run(store.upsert_event(ev))
    with pytest.raises(ValueError):
        run(store.set_event_title(111, eid, "Invalid change"))
    assert run(store.get_event(eid))["title"] == "Scraped title"


def test_event_closing_during_title_prompt_is_denied(store):
    eid = run(store.upsert_event(event()))
    ctx = SimpleNamespace(user_data={})
    run(handlers.on_callback(update(111, f"ef:{eid}:title"), ctx))
    run(store.conn.execute("UPDATE events SET end_ts=1 WHERE id=?", (eid,)))
    run(store.conn.commit())
    run(handlers.on_text(update(111, text="Too late"), ctx))
    assert run(store.get_event(eid))["title"] == "Scraped title"


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_invalid_title_is_rejected(store, title):
    eid = run(store.upsert_event(event()))
    with pytest.raises(ValueError):
        run(store.set_event_title(111, eid, title))
    assert run(store.get_event(eid))["title"] == "Scraped title"


def test_current_notifications_exclude_revoked_admins(store):
    run(store.add_admin(111, 222))
    run(store.add_admin(111, 333))
    run(store.remove_admin(111, 222))
    assert run(store.notification_ids()) == [111, 333]


def test_legacy_database_migration_preserves_event(store):
    run(store.close())
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("DROP TABLE events")
        conn.executescript(database.SCHEMA.replace("  custom_title TEXT,\n", ""))
        conn.execute("INSERT INTO events(url,title,created_at) VALUES(?,?,?)", ("https://www.binance.com/old", "Old event", 1))
    run(store.init())
    ev = run(store.list_events())[0]
    assert ev["title"] == "Old event"
    run(store.set_event_title(111, ev["id"], "New title"))
    assert run(store.get_event(ev["id"]))["title"] == "New title"