"""Unit tests for the pure aggregation layer.

Anchor date: Monday 2026-07-20 (UTC). Timezone facts used:
- Asia/Tokyo is UTC+9 year-round, Asia/Kolkata UTC+5:30.
- Europe/London springs forward 2026-03-29 01:00 UTC (01:00 -> 02:00 local)
  and falls back 2026-10-25 01:00 UTC (02:00 -> 01:00 local).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from iris import analysis

UTC = timezone.utc
TOKYO = ZoneInfo("Asia/Tokyo")
KOLKATA = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")


def _epoch(*args, tz=UTC) -> int:
    return int(datetime(*args, tzinfo=tz).timestamp())


# -- message bucketing --------------------------------------------------------

def test_message_weekday_and_hour_follow_target_tz():
    # Monday 23:30 UTC is Tuesday 08:30 in Tokyo.
    ts = [_epoch(2026, 7, 20, 23, 30)]
    utc_grid = analysis.message_grid(ts, UTC)
    assert utc_grid[0][23] == 1  # Monday, hour 23
    tokyo_grid = analysis.message_grid(ts, TOKYO)
    assert tokyo_grid[1][8] == 1  # Tuesday, hour 8
    assert sum(map(sum, tokyo_grid)) == 1


def test_grid_helpers():
    ts = [_epoch(2026, 7, 20, 9), _epoch(2026, 7, 20, 9, 59), _epoch(2026, 7, 21, 9)]
    grid = analysis.message_grid(ts, UTC)
    assert analysis.hour_totals(grid)[9] == 3
    assert analysis.weekday_totals(grid) == [2, 1, 0, 0, 0, 0, 0]
    assert analysis.day_slice(grid, 0)[9] == 2


# -- voice bucketing ----------------------------------------------------------

def test_voice_minutes_split_across_hours():
    # 14:30-16:15 UTC -> 30 min to hour 14, 60 to 15, 15 to 16.
    session = (_epoch(2026, 7, 20, 14, 30), _epoch(2026, 7, 20, 16, 15))
    hours = analysis.hour_totals(analysis.voice_grid([session], UTC))
    assert hours[14] == 30 and hours[15] == 60 and hours[16] == 15
    assert sum(hours) == 105


def test_voice_fractional_offset():
    # 14:00-15:00 UTC is 19:30-20:30 in Kolkata (+5:30).
    session = (_epoch(2026, 7, 20, 14), _epoch(2026, 7, 20, 15))
    hours = analysis.hour_totals(analysis.voice_grid([session], KOLKATA))
    assert hours[19] == 30 and hours[20] == 30


def test_voice_dst_spring_forward_conserves_minutes():
    # 00:30-01:30 UTC spans London's 2026 spring-forward: local wall time jumps
    # from 00:59 GMT to 02:00 BST, so local hour 1 gets nothing.
    session = (_epoch(2026, 3, 29, 0, 30), _epoch(2026, 3, 29, 1, 30))
    hours = analysis.hour_totals(analysis.voice_grid([session], LONDON))
    assert hours[0] == 30 and hours[1] == 0 and hours[2] == 30
    assert sum(hours) == 60


def test_voice_dst_fall_back_conserves_minutes():
    # 00:30-01:30 UTC spans London's fall-back: both halves land in local hour 1.
    session = (_epoch(2026, 10, 25, 0, 30), _epoch(2026, 10, 25, 1, 30))
    hours = analysis.hour_totals(analysis.voice_grid([session], LONDON))
    assert hours[1] == 60
    assert sum(hours) == 60


def test_voice_session_crossing_midnight_splits_weekdays():
    # Mon 23:30 -> Tue 00:30 UTC.
    session = (_epoch(2026, 7, 20, 23, 30), _epoch(2026, 7, 21, 0, 30))
    grid = analysis.voice_grid([session], UTC)
    assert grid[0][23] == 30 and grid[1][0] == 30


def test_voice_degenerate_sessions():
    ts = _epoch(2026, 7, 20, 12)
    assert sum(map(sum, analysis.voice_grid([(ts, ts)], UTC))) == 0
    assert sum(map(sum, analysis.voice_grid([(ts, ts - 100)], UTC))) == 0  # clamped


# -- active dates & summary ---------------------------------------------------

def test_active_dates_counts_spanned_days():
    msgs = [_epoch(2026, 7, 20, 10)]
    sessions = [(_epoch(2026, 7, 21, 23, 30), _epoch(2026, 7, 22, 0, 30))]
    assert len(analysis.active_dates(msgs, sessions, UTC)) == 3


def test_summary_fields():
    msgs = [_epoch(2026, 7, 20, 18, 5), _epoch(2026, 7, 20, 18, 40), _epoch(2026, 7, 21, 9)]
    sessions = [
        (_epoch(2026, 7, 20, 20), _epoch(2026, 7, 20, 22)),   # 2h
        (_epoch(2026, 7, 21, 20), _epoch(2026, 7, 21, 20, 30)),  # 30m
    ]
    s = analysis.summary(msgs, sessions, UTC)
    assert s["total_messages"] == 3
    assert s["total_vc_seconds"] == 2.5 * 3600
    assert s["session_count"] == 2
    assert s["longest_session_seconds"] == 2 * 3600
    assert s["avg_session_seconds"] == 1.25 * 3600
    assert s["most_active_hour"] == 18      # chat share 2/3 beats VC's 90/150
    assert s["most_active_weekday"] == 0    # Monday: 2 msgs of 3 + 2h VC of 2.5
    assert s["tracked_since"] == datetime(2026, 7, 20, tzinfo=UTC).date()
    assert s["last_active_utc"] == _epoch(2026, 7, 21, 20, 30)
    assert s["active_days"] == 2
    assert abs(s["vc_seconds_per_message"] - 2.5 * 3600 / 3) < 1e-9


# -- game totals --------------------------------------------------------------

def test_game_totals_sums_and_sorts_by_time():
    sessions = [
        ("VALORANT", 0, 3600),          # 1h
        ("osu!", 0, 600),               # 10m
        ("VALORANT", 10_000, 12_700),   # +45m -> 1h45m total, 2 sessions
    ]
    assert analysis.game_totals(sessions) == [
        ("VALORANT", 6300, 2),
        ("osu!", 600, 1),
    ]


def test_game_totals_clamps_negatives_and_breaks_ties_alphabetically():
    sessions = [
        ("Zed", 100, 1_100),      # 1000s
        ("Abyss", 0, 1_000),      # 1000s — ties Zed, sorts first by name
        ("Broken", 500, 200),     # negative length -> clamped to 0
    ]
    assert analysis.game_totals(sessions) == [
        ("Abyss", 1000, 1),
        ("Zed", 1000, 1),
        ("Broken", 0, 1),
    ]


def test_game_totals_empty():
    assert analysis.game_totals([]) == []


def test_reconstruct_sessions_pairs_joins_and_leaves():
    events = [
        (1000, 1, 50, "join"),
        (1600, 1, 50, "leave"),
        (1200, 2, 50, "join"),
        (1500, 2, 50, "leave"),
    ]
    sessions, ignored = analysis.reconstruct_sessions(events)
    assert sorted(sessions) == [(1, 50, 1000, 1600), (2, 50, 1200, 1500)]
    assert ignored == 0


def test_reconstruct_sessions_drops_unpaired_events():
    events = [
        (1000, 1, 50, "leave"),  # leave with no join (log starts mid-session)
        (2000, 1, 50, "join"),   # join never followed by a leave
    ]
    sessions, ignored = analysis.reconstruct_sessions(events)
    assert sessions == []
    assert ignored == 2


def test_reconstruct_sessions_join_while_open_is_a_move():
    events = [
        (1000, 1, 50, "join"),
        (1300, 1, 60, "join"),   # moved channels; leave for 50 never logged
        (1900, 1, 60, "leave"),
    ]
    sessions, _ = analysis.reconstruct_sessions(events)
    assert sessions == [(1, 50, 1000, 1300), (1, 60, 1300, 1900)]


def test_reconstruct_sessions_tie_leaves_sort_first():
    # A move logged as leave+join in the same second must not corrupt state.
    events = [
        (1000, 1, 50, "join"),
        (1500, 1, 60, "join"),
        (1500, 1, 50, "leave"),
        (2000, 1, 60, "leave"),
    ]
    sessions, ignored = analysis.reconstruct_sessions(events)
    assert sessions == [(1, 50, 1000, 1500), (1, 60, 1500, 2000)]
    assert ignored == 0


def test_reconstruct_sessions_cutoff_clips_and_drops():
    events = [
        (1000, 1, 50, "join"),
        (3000, 1, 50, "leave"),  # spans the cutoff -> clipped to it
        (4000, 2, 50, "join"),
        (5000, 2, 50, "leave"),  # entirely after cutoff -> dropped
    ]
    sessions, ignored = analysis.reconstruct_sessions(events, cutoff=2000)
    assert sessions == [(1, 50, 1000, 2000)]
    assert ignored == 1


def test_summary_empty():
    s = analysis.summary([], [], UTC)
    assert s["total_messages"] == 0
    assert s["most_active_hour"] is None
    assert s["most_active_weekday"] is None
    assert s["tracked_since"] is None
    assert s["last_active_utc"] is None
    assert s["vc_seconds_per_message"] is None
    assert s["active_days"] == 0
