"""Storage repository tests against a real temporary SQLite file."""
import asyncio

from iris.storage import Storage


def test_storage_flow(tmp_path):
    asyncio.run(_flow(str(tmp_path / "test.db")))


def test_game_session_flow(tmp_path):
    asyncio.run(_game_flow(str(tmp_path / "games.db")))


def test_unmute_shield_flow(tmp_path):
    asyncio.run(_unmute_flow(str(tmp_path / "unmute.db")))


async def _unmute_flow(db_path: str) -> None:
    s = Storage(db_path)
    await s.open()

    # a shield is one row per member, restartable and guild-scoped
    assert await s.get_active_unmute_shields(1000) == []
    await s.grant_unmute_shield(10, 1, granted_by=2, granted_utc=1000, expires_utc=1600)
    await s.grant_unmute_shield(99, 1, granted_by=2, granted_utc=1000, expires_utc=1600)
    assert sorted(await s.get_active_unmute_shields(1000)) == [(10, 1, 1600), (99, 1, 1600)]

    # re-granting extends the same row rather than stacking a second one
    await s.grant_unmute_shield(10, 1, granted_by=3, granted_utc=1500, expires_utc=2100)
    assert sorted(await s.get_active_unmute_shields(1500)) == [(10, 1, 2100), (99, 1, 1600)]

    # expiry is exclusive of "now", and the sweep only takes what's run out
    assert await s.get_active_unmute_shields(1600) == [(10, 1, 2100)]
    assert await s.purge_expired_unmute_shields(1600) == 1
    assert await s.get_active_unmute_shields(1000) == [(10, 1, 2100)]

    await s.clear_unmute_shield(10, 1)
    assert await s.get_active_unmute_shields(1000) == []

    # daily-use stamps: per member, per guild, last write wins
    assert await s.get_last_unmute_use(10, 2) is None
    await s.record_unmute_use(10, 2, 1000)
    assert await s.get_last_unmute_use(10, 2) == 1000
    assert await s.get_last_unmute_use(99, 2) is None
    await s.record_unmute_use(10, 2, 90_000)
    assert await s.get_last_unmute_use(10, 2) == 90_000

    await s.close()


async def _game_flow(db_path: str) -> None:
    s = Storage(db_path)
    await s.open()

    # open sessions are invisible to reads until closed
    await s.open_game_session(1, 10, "VALORANT", 1000)
    assert await s.get_game_sessions(1, 10) == []
    assert await s.get_open_game_sessions() == [(1, 10, "VALORANT", 1000)]
    await s.close_game_session(1, 10, "VALORANT", 1600)
    assert await s.get_game_sessions(1, 10) == [("VALORANT", 1000, 1600)]

    # two games at once are tracked independently
    await s.open_game_session(1, 10, "VALORANT", 2000)
    await s.open_game_session(1, 10, "Spotify-less: Deep Rock", 2000)
    assert len(await s.get_open_game_sessions()) == 2
    await s.close_game_session(1, 10, "VALORANT", 2500)
    # the other game is still open, so still hidden from reads
    assert await s.get_game_sessions(1, 10) == [("VALORANT", 1000, 1600),
                                                ("VALORANT", 2000, 2500)]

    # heartbeat + crash recovery: reconcile closes at last heartbeat
    await s.heartbeat_games([(1, "Deep Rock")], 2600)  # non-matching game: no-op
    await s.heartbeat_games([(1, "Spotify-less: Deep Rock")], 2700)
    assert await s.reconcile_open_game_sessions(9999) == 1
    assert ("Spotify-less: Deep Rock", 2000, 2700) in await s.get_game_sessions(1, 10)

    # double-open defence: re-opening the same game closes the stale row at its
    # last heartbeat, leaving exactly one open row (and one short closed one)
    await s.open_game_session(2, 10, "osu!", 3000)
    await s.heartbeat_games([(2, "osu!")], 3050)
    await s.open_game_session(2, 10, "osu!", 3100)
    open_rows = await s.get_open_game_sessions()
    assert len(open_rows) == 1 and open_rows[0][0] == 2
    assert ("osu!", 3000, 3050) in await s.get_game_sessions(2, 10)

    # graceful shutdown closes everything still open at "now"
    await s.close_all_open_game_sessions(3200)
    assert await s.get_open_game_sessions() == []
    assert ("osu!", 3100, 3200) in await s.get_game_sessions(2, 10)

    # guild scoping + since filter
    await s.open_game_session(1, 99, "VALORANT", 4000)
    await s.close_game_session(1, 99, "VALORANT", 4100)
    assert await s.get_game_sessions(1, 99) == [("VALORANT", 4000, 4100)]
    # since filters on end_utc; the 1000-1600 session drops out
    assert set(await s.get_game_sessions(1, 10, since=2000)) == {
        ("VALORANT", 2000, 2500), ("Spotify-less: Deep Rock", 2000, 2700)
    }

    # optout purges game history too, across guilds, leaving others untouched
    before_optout = await s.get_game_sessions(2, 10)
    await s.set_optout(1)
    assert await s.get_game_sessions(1, 10) == []
    assert await s.get_game_sessions(1, 99) == []
    assert await s.get_game_sessions(2, 10) == before_optout

    await s.close()


async def _flow(db_path: str) -> None:
    s = Storage(db_path)
    await s.open()

    # timezone round trip
    assert await s.get_timezone(1) is None
    await s.set_timezone(1, "Europe/London")
    assert await s.get_timezone(1) == "Europe/London"
    await s.set_timezone(1, "Asia/Tokyo")
    assert await s.get_timezone(1) == "Asia/Tokyo"
    await s.clear_timezone(1)
    assert await s.get_timezone(1) is None

    # messages, guild-scoped, since filter
    await s.log_message(1, 10, 100, 1000)
    await s.log_message(1, 10, 101, 2000)
    await s.log_message(2, 10, 100, 1500)
    await s.log_message(1, 99, 100, 1700)
    assert [ts for _, ts in await s.get_messages(1, 10)] == [1000, 2000]
    assert [ts for _, ts in await s.get_messages(1, 10, since=1500)] == [2000]

    # bulk backfill: message ids dedupe batch re-runs and live-capture overlap
    rows = [
        (111, 3, 10, 500, 3000),
        (112, 3, 10, 500, 3100),
        (112, 3, 10, 500, 3100),  # duplicate id within one batch
    ]
    assert await s.log_messages_bulk(rows) == 2
    assert await s.log_messages_bulk(rows) == 0  # full re-run is a no-op
    await s.log_message(3, 10, 500, 3100, message_id=112)  # live overlap ignored
    assert [ts for _, ts in await s.get_messages(3, 10)] == [3000, 3100]

    # legacy (NULL-id) rows are purged per channel once backfill re-records it
    await s.log_message(3, 10, 500, 3200)  # pre-id live capture, same channel
    await s.log_message(3, 10, 999, 3300)  # other channel must be untouched
    await s.purge_legacy_messages(10, 500)
    assert [ts for _, ts in await s.get_messages(3, 10)] == [3000, 3100, 3300]

    # voice lifecycle: open sessions are invisible to reads until closed
    await s.open_voice_session(1, 10, 200, 5000)
    assert await s.get_voice_sessions(1, 10) == []
    assert len(await s.get_open_sessions()) == 1
    await s.heartbeat([1], 5060)
    await s.close_voice_session(1, 10, 5100)
    assert await s.get_voice_sessions(1, 10) == [(200, 5000, 5100)]

    # crash recovery: reconcile closes at last heartbeat
    await s.open_voice_session(1, 10, 200, 6000)
    await s.heartbeat([1], 6120)
    assert await s.reconcile_open_sessions(9999) == 1
    assert (200, 6000, 6120) in await s.get_voice_sessions(1, 10)

    # never-heartbeated session reconciles to its start
    await s.open_voice_session(1, 10, 200, 6500)
    await s.reconcile_open_sessions(9999)
    assert (200, 6500, 6500) in await s.get_voice_sessions(1, 10)

    # double-open defence: re-opening closes the stale session first
    await s.open_voice_session(1, 10, 200, 7000)
    await s.open_voice_session(1, 10, 201, 7100)
    open_rows = await s.get_open_sessions()
    assert len(open_rows) == 1 and open_rows[0][2] == 201

    # graceful shutdown closes everything at "now"
    await s.close_all_open_sessions(7200)
    assert await s.get_open_sessions() == []
    assert (201, 7100, 7200) in await s.get_voice_sessions(1, 10)

    # imported sessions: tagged source, replaceable wholesale, capped by the
    # earliest live session
    assert await s.earliest_live_voice_start(10) == 5000  # first live session above
    assert await s.add_voice_sessions_bulk(10, [(4, 300, 100, 700), (4, 300, 900, 1200)]) == 2
    assert len(await s.get_voice_sessions(4, 10)) == 2  # reads merge both sources
    assert await s.delete_voice_sessions_by_source(10, "backlog") == 2
    assert await s.get_voice_sessions(4, 10) == []
    # live rows are untouched by a backlog replace
    assert (200, 5000, 5100) in await s.get_voice_sessions(1, 10)

    # optout purges history and flags the user
    assert not await s.is_opted_out(1)
    await s.set_optout(1)
    assert await s.is_opted_out(1)
    assert await s.get_messages(1, 10) == []
    assert await s.get_voice_sessions(1, 10) == []
    assert await s.get_opted_out_ids() == {1}
    # other users' data untouched
    assert len(await s.get_messages(2, 10)) == 1
    await s.set_optin(1)
    assert not await s.is_opted_out(1)
    assert await s.get_opted_out_ids() == set()

    # backup produces a self-contained, openable snapshot
    backup_path = db_path + ".backup"
    await s.backup_to(backup_path)
    b = Storage(backup_path)
    await b.open()
    assert len(await b.get_messages(2, 10)) == 1
    await b.close()

    await s.close()
