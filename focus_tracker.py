
import requests
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

BASE_URL = "http://localhost:5600/api/0/buckets"

WINDOW_BUCKET = "aw-watcher-window_RAVI"
AFK_BUCKET = "aw-watcher-afk_RAVI"

DEVELOPER_APPS = [
    "code.exe",
    "pycharm",
    "intellij",
    "idea",
    "eclipse",
    "postman",
    "powershell",
    "cmd.exe",
    "windows terminal",
    "terminal",
    "git bash"
]

DEVELOPER_TITLES = [
    "developer tools",
    "devtools"
]


def get_events(bucket_id, start_time, end_time):
    """Fetch ActivityWatch events for a selected time range."""
    url = f"{BASE_URL}/{bucket_id}/events"

    params = {
        "start": start_time.isoformat(),
        "end": end_time.isoformat()
    }

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()

    return response.json()


def is_developer_app(event):
    """Identify events from developer applications."""
    data = event.get("data", {})

    app = str(data.get("app", "")).lower()
    title = str(data.get("title", "")).lower()

    return (
        any(name in app for name in DEVELOPER_APPS)
        or any(name in title for name in DEVELOPER_TITLES)
    )


def parse_timestamp(value):
    """Convert an ISO timestamp to a timezone-aware datetime."""
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return timestamp


def merge_intervals(intervals):
    """Merge overlapping or adjacent time intervals."""
    if not intervals:
        return []

    intervals = sorted(intervals, key=lambda item: item[0])
    merged = [intervals[0]]

    for current_start, current_end in intervals[1:]:
        previous_start, previous_end = merged[-1]

        if current_start <= previous_end:
            merged[-1] = (
                previous_start,
                max(previous_end, current_end)
            )
        else:
            merged.append((current_start, current_end))

    return merged


def subtract_intervals(source_start, source_end, excluded_intervals):
    """Remove excluded intervals from one source interval."""
    active_parts = [(source_start, source_end)]

    for excluded_start, excluded_end in excluded_intervals:
        remaining = []

        for part_start, part_end in active_parts:
            if (
                excluded_end <= part_start
                or excluded_start >= part_end
            ):
                remaining.append((part_start, part_end))
            else:
                if part_start < excluded_start:
                    remaining.append((part_start, excluded_start))

                if excluded_end < part_end:
                    remaining.append((excluded_end, part_end))

        active_parts = remaining

        if not active_parts:
            break

    return active_parts


def calculate_focus(window_events, afk_events, start_time, end_time):
    """Calculate focus time per app while excluding AFK intervals."""
    app_intervals = defaultdict(list)
    afk_intervals = []


    for event in afk_events:
        data = event.get("data", {})

        if data.get("status") != "afk":
            continue

        event_start = parse_timestamp(event["timestamp"])
        duration = max(0.0, float(event.get("duration", 0)))
        event_end = event_start + timedelta(seconds=duration)

        event_start = max(event_start, start_time)
        event_end = min(event_end, end_time)

        if event_end > event_start:
            afk_intervals.append((event_start, event_end))

    merged_afk = merge_intervals(afk_intervals)


    for event in window_events:
        if not is_developer_app(event):
            continue

        data = event.get("data", {})
        app = str(data.get("app", "Unknown"))

        event_start = parse_timestamp(event["timestamp"])
        duration = max(0.0, float(event.get("duration", 0)))
        event_end = event_start + timedelta(seconds=duration)

        event_start = max(event_start, start_time)
        event_end = min(event_end, end_time)

        if event_end > event_start:
            app_intervals[app].append((event_start, event_end))

    
    focus_seconds = defaultdict(float)

    for app, intervals in app_intervals.items():
        merged_app_intervals = merge_intervals(intervals)

        for app_start, app_end in merged_app_intervals:
            active_parts = subtract_intervals(
                app_start,
                app_end,
                merged_afk
            )

            for part_start, part_end in active_parts:
                focus_seconds[app] += (
                    part_end - part_start
                ).total_seconds()


    all_developer_intervals = []

    for intervals in app_intervals.values():
        all_developer_intervals.extend(intervals)

    unique_developer_intervals = merge_intervals(
        all_developer_intervals
    )

    unique_focus_seconds = 0.0

    for interval_start, interval_end in unique_developer_intervals:
        active_parts = subtract_intervals(
            interval_start,
            interval_end,
            merged_afk
        )

        for part_start, part_end in active_parts:
            unique_focus_seconds += (
                part_end - part_start
            ).total_seconds()

    return focus_seconds, merged_afk, unique_focus_seconds


def main():
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=24)

    try:
        window_events = get_events(
            WINDOW_BUCKET, start_time, end_time
        )

        afk_events = get_events(
            AFK_BUCKET, start_time, end_time
        )

        developer_events = [
            event for event in window_events
            if is_developer_app(event)
        ]

        focus_seconds, afk_intervals, unique_focus_seconds = (
            calculate_focus(
                window_events,
                afk_events,
                start_time,
                end_time
            )
        )

        summary = {
            "period_start": start_time.isoformat(),
            "period_end": end_time.isoformat(),
            "window_events_fetched": len(window_events),
            "afk_events_fetched": len(afk_events),
            "developer_events_found": len(developer_events),
            "afk_intervals_found": len(afk_intervals),
            "focused_minutes_by_application": {
                app: round(seconds / 60, 2)
                for app, seconds in sorted(focus_seconds.items())
            },
            "total_unique_focused_minutes": round(
                unique_focus_seconds / 60, 2
            ),
            "calculation_note": (
                "Per-app minutes can overlap across different apps. "
                "total_unique_focused_minutes merges developer intervals "
                "across all apps before excluding AFK time."
            )
        }

        print("\nDeveloper Focus Summary")
        print("=" * 35)
        print(json.dumps(summary, indent=4))

        with open(
            "focus_summary.json", "w", encoding="utf-8"
        ) as file:
            json.dump(summary, file, indent=4)

        print("\nSummary saved to focus_summary.json")

    except requests.RequestException as error:
        print("ActivityWatch API request failed:", error)

    except (KeyError, TypeError, ValueError) as error:
        print("Could not process ActivityWatch events:", error)


if __name__ == "__main__":
    main()
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           