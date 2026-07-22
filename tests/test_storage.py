"""Storage repository tests against a real temporary SQLite file."""
import asyncio

from iris.storage import Storage


def test_storage_flow(tmp_path):
    asyncio.run(_flow(str(tmp_path / "test.db")))


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

    await s.close()
