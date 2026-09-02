"""Pure aggregation: raw rows + a target timezone -> bucketed data.

No I/O, no database, no matplotlib. Everything here is unit-testable.

Inputs are plain sequences: message timestamps as epoch-UTC ints, voice
sessions as (start_utc, end_utc) pairs (closed sessions only). Buckets are
computed AFTER converting each instant to the target timezone, so fractional
offsets and DST land minutes in the right local hour and weekday.

Grids are 7x24 lists indexed [weekday][hour], weekday 0 = Monday.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Iterable, Iterator, Sequence

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

Grid = list[list[float]]


def _empty_grid() -> Grid:
    return [[0.0] * 24 for _ in range(7)]


def message_grid(ts_list: Iterable[int], tz: tzinfo) -> Grid:
    """Message counts per [weekday][hour], local to `tz`."""
    grid = _empty_grid()
    for ts in ts_list:
        local = datetime.fromtimestamp(ts, tz)
        grid[local.weekday()][local.hour] += 1
    return grid


def _walk_session(start_utc: int, end_utc: int, tz: tzinfo) -> Iterator[tuple[datetime, float]]:
    """Yield (local_segment_start, minutes) for each local-hour segment a
    session spans, e.g. 14:30-16:15 -> (14:30, 30), (15:00, 60), (16:00, 15).

    Walks in epoch seconds and reconverts at each hour boundary, so DST
    transitions and fractional offsets can't misassign minutes.
    """
    epoch = start_utc
    end = max(end_utc, start_utc)
    while epoch < end:
        local = datetime.fromtimestamp(epoch, tz)
        to_boundary = 3600 - (local.minute * 60 + local.second)
        seg_end = min(epoch + max(to_boundary, 1), end)
        yield local, (seg_end - epoch) / 60.0
        epoch = seg_end


def voice_grid(sessions: Iterable[tuple[int, int]], tz: tzinfo) -> Grid:
    """Voice minutes per [weekday][hour], local to `tz`."""
    grid = _empty_grid()
    for start, end in sessions:
        for local, minutes in _walk_session(start, end, tz):
            grid[local.weekday()][local.hour] += minutes
    return grid


def hour_totals(grid: Grid) -> list[float]:
    return [sum(grid[d][h] for d in range(7)) for h in range(24)]


def weekday_totals(grid: Grid) -> list[float]:
    return [sum(grid[d]) for d in range(7)]


def day_slice(grid: Grid, weekday: int) -> list[float]:
    return list(grid[weekday])


def active_dates(
    ts_list: Iterable[int], sessions: Iterable[tuple[int, int]], tz: tzinfo
) -> set[date]:
    """Distinct local dates with any activity (a message or any VC minute)."""
    dates: set[date] = set()
    for ts in ts_list:
        dates.add(datetime.fromtimestamp(ts, tz).date())
    for start, end in sessions:
        for local, _ in _walk_session(start, end, tz):
            dates.add(local.date())
    return dates


def _argmax_combined_share(a: Sequence[float], b: Sequence[float]) -> int | None:
    """Index where combined activity peaks. Each series is normalised to its
    own total first, so a heavy VC user and a heavy chatter weigh equally."""
    ta, tb = sum(a), sum(b)
    if ta == 0 and tb == 0:
        return None
    shares = [
        (a[i] / ta if ta else 0.0) + (b[i] / tb if tb else 0.0) for i in range(len(a))
    ]
    return max(range(len(shares)), key=shares.__getitem__)


def summary(
    ts_list: Sequence[int], sessions: Sequence[tuple[int, int]], tz: tzinfo
) -> dict:
    """Everything the /stats card shows, as plain numbers (no formatting)."""
    msg_g = message_grid(ts_list, tz)
    vc_g = voice_grid(sessions, tz)

    durations = [max(end - start, 0) for start, end in sessions]
    total_vc_seconds = sum(durations)
    total_messages = len(ts_list)

    event_starts = list(ts_list) + [s for s, _ in sessions]
    event_ends = list(ts_list) + [e for _, e in sessions]

    return {
        "total_messages": total_messages,
        "total_vc_seconds": total_vc_seconds,
        "session_count": len(sessions),
        "longest_session_seconds": max(durations, default=0),
        "avg_session_seconds": (total_vc_seconds / len(durations)) if durations else 0,
        "vc_seconds_per_message": (
            total_vc_seconds / total_messages if total_messages else None
        ),
        "most_active_hour": _argmax_combined_share(hour_totals(msg_g), hour_totals(vc_g)),
        "most_active_weekday": _argmax_combined_share(
            weekday_totals(msg_g), weekday_totals(vc_g)
        ),
        "tracked_since": (
            datetime.fromtimestamp(min(event_starts), tz).date() if event_starts else None
        ),
        "last_active_utc": max(event_ends, default=None),
        "active_days": len(active_dates(ts_list, sessions, tz)),
    }


VoiceEvent = tuple[int, int, int, str]  # (ts_utc, user_id, channel_id, 'join'|'leave')


def uncovered_spans(
    start: int, end: int, covered: Sequence[tuple[int, int]]
) -> Iterator[tuple[int, int]]:
    """Yield the pieces of [start, end) that fall outside `covered` (sorted,
    merged, non-overlapping spans). A session straddling a coverage gap comes
    back as several pieces; a fully covered one yields nothing."""
    cursor = start
    for c_start, c_end in covered:
        if c_end <= cursor:
            continue
        if c_start >= end:
            break
        if c_start > cursor:
            yield (cursor, c_start)
        cursor = c_end
        if cursor >= end:
            return
    if cursor < end:
        yield (cursor, end)


def reconstruct_sessions(
    events: Iterable[VoiceEvent], covered: Sequence[tuple[int, int]] | None = None
) -> tuple[list[tuple[int, int, int, int]], int]:
    """Pair join/leave events (e.g. parsed from a logging bot's history) into
    (user_id, channel_id, start_utc, end_utc) sessions.

    Rules:
    - A join while a session is open closes it at that instant (a move).
    - A leave with no open session, or a join never followed by a leave,
      is unknowable and dropped (counted in the returned `ignored`).
    - At equal timestamps a leave sorts before a join, so move pairs and
      instant rejoins resolve correctly.
    - Sessions are trimmed against `covered`, the spans live capture already
      recorded. Only the uncovered remainder is kept, so downtime between two
      live spans still backfills while live time is never counted twice. A
      session lying entirely inside a covered span is dropped (counted in
      `ignored`).
    """
    open_sessions: dict[int, tuple[int, int]] = {}  # user_id -> (start, channel)
    sessions: list[tuple[int, int, int, int]] = []
    ignored = 0
    for ts, user_id, channel_id, kind in sorted(
        events, key=lambda e: (e[0], e[1], 0 if e[3] == "leave" else 1)
    ):
        if kind == "join":
            if user_id in open_sessions:
                start, chan = open_sessions.pop(user_id)
                if ts > start:
                    sessions.append((user_id, chan, start, ts))
            open_sessions[user_id] = (ts, channel_id)
        elif user_id in open_sessions:
            start, chan = open_sessions.pop(user_id)
            if ts > start:
                sessions.append((user_id, chan, start, ts))
        else:
            ignored += 1
    ignored += len(open_sessions)

    if covered:
        trimmed = []
        for user_id, chan, start, end in sessions:
            pieces = list(uncovered_spans(start, end, covered))
            if not pieces:
                ignored += 1
                continue
            trimmed += [(user_id, chan, s, e) for s, e in pieces]
        sessions = trimmed
    return sessions, ignored


UTC = timezone.utc
