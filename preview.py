"""Render sample Iris charts with fake data — visual iteration without Discord.

Usage: python preview.py   (outputs PNGs to ./preview_out/)
"""
import random
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from iris.bot import _build_activity_png, _build_stats_png

OUT = Path(__file__).parent / "preview_out"
UTC = timezone.utc

# Evening-skewed hour weights (index = UTC hour).
HOUR_WEIGHTS = [2, 1, 0.5, 0.3, 0.2, 0.3, 0.5, 1, 2, 3, 3, 3.5,
                4, 4, 3.5, 3, 4, 5, 7, 9, 10, 9, 6, 3.5]


def fake_data(days: int = 120) -> tuple[list[int], list[tuple[int, int]]]:
    random.seed(7)
    end = int(datetime(2026, 7, 22, tzinfo=UTC).timestamp())
    msgs: list[int] = []
    sessions: list[tuple[int, int]] = []
    for day in range(days):
        day_start = end - (day + 1) * 86400
        weekend_boost = 1.6 if datetime.fromtimestamp(day_start, UTC).weekday() >= 5 else 1.0
        for _ in range(int(random.randint(3, 45) * weekend_boost)):
            hour = random.choices(range(24), weights=HOUR_WEIGHTS)[0]
            msgs.append(day_start + hour * 3600 + random.randint(0, 3599))
        for _ in range(random.choice([0, 0, 1, 1, 2])):
            start_hour = random.choice([17, 18, 19, 20, 20, 21, 21, 22, 23])
            start = day_start + start_hour * 3600 + random.randint(0, 3599)
            sessions.append((start, start + random.randint(25, 240) * 60))
    return msgs, sessions


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tz = ZoneInfo("Europe/London")
    msgs, sessions = fake_data()

    renders = {
        "activity.png": _build_activity_png(
            "moonlace", msgs, sessions, tz, "Europe/London", None),
        "activity_friday.png": _build_activity_png(
            "moonlace", msgs, sessions, tz, "Europe/London", 4),
        "activity_no_vc.png": _build_activity_png(
            "quietone", msgs[:400], [], tz, "Europe/London", None),
        "stats.png": _build_stats_png(
            "moonlace", msgs, sessions, tz, "Europe/London", date(2024, 11, 3)),
    }
    for filename, buf in renders.items():
        (OUT / filename).write_bytes(buf.getvalue())
        print("wrote", OUT / filename)


if __name__ == "__main__":
    main()
