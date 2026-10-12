import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import firebase_admin
import requests
import schedule
from dotenv import load_dotenv
from firebase_admin import credentials, firestore




PROJECT_DIR = Path(__file__).resolve().parent

FALLBACK_FILE = PROJECT_DIR / "focus_fallback.jsonl"
LOG_FILE = PROJECT_DIR / "focus_sync.log"

COLLECTION_NAME = "dev_focus_metrics"

ACTIVITYWATCH_URL = "http://localhost:5600/api/0"

load_dotenv(PROJECT_DIR / ".env")

CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)




def connect_firebase():
    """Connect to Firebase Firestore."""

    if not CREDENTIALS_PATH:
        raise ValueError(
            "FIREBASE_CREDENTIALS_PATH is missing from the .env file."
        )

    credentials_file = Path(CREDENTIALS_PATH)

    if not credentials_file.is_absolute():
        credentials_file = PROJECT_DIR / credentials_file

    if not credentials_file.exists():
        raise FileNotFoundError(
            f"Firebase credentials file not found: {credentials_file}"
        )

    if not firebase_admin._apps:
        firebase_credentials = credentials.Certificate(
            str(credentials_file)
        )

        firebase_admin.initialize_app(firebase_credentials)

    return firestore.client()


db = connect_firebase()



def get_window_events(start_time, end_time):
    """
    Retrieve window activity events from ActivityWatch
    for the requested time interval.
    """

    buckets_response = requests.get(
        f"{ACTIVITYWATCH_URL}/buckets/",
        timeout=15
    )

    buckets_response.raise_for_status()
    buckets = buckets_response.json()

    window_bucket_ids = [
        bucket_id
        for bucket_id, bucket in buckets.items()
        if bucket.get("type") == "currentwindow"
    ]

    if not window_bucket_ids:
        logging.warning("No current-window ActivityWatch bucket found.")
        return []

    all_events = []

    for bucket_id in window_bucket_ids:
        response = requests.get(
            f"{ACTIVITYWATCH_URL}/buckets/"
            f"{bucket_id}/events",
            params={
                "start": start_time.isoformat(),
                "end": end_time.isoformat()
            },
            timeout=30
        )

        response.raise_for_status()

        events = response.json()

        for event in events:
            event["bucket_id"] = bucket_id
            all_events.append(event)

    return all_events




def get_afk_events(start_time, end_time):
    """Retrieve away-from-keyboard events."""

    buckets_response = requests.get(
        f"{ACTIVITYWATCH_URL}/buckets/",
        timeout=15
    )

    buckets_response.raise_for_status()
    buckets = buckets_response.json()

    afk_bucket_ids = [
        bucket_id
        for bucket_id, bucket in buckets.items()
        if bucket.get("type") == "afkstatus"
    ]

    if not afk_bucket_ids:
        logging.warning("No AFK ActivityWatch bucket found.")
        return []

    all_events = []

    for bucket_id in afk_bucket_ids:
        response = requests.get(
            f"{ACTIVITYWATCH_URL}/buckets/"
            f"{bucket_id}/events",
            params={
                "start": start_time.isoformat(),
                "end": end_time.isoformat()
            },
            timeout=30
        )

        response.raise_for_status()

        events = response.json()

        for event in events:
            event["bucket_id"] = bucket_id
            all_events.append(event)

    return all_events




def calculate_hourly_focus(start_time, end_time):
    """
    Calculate application usage for one specific hour.

    Window event durations are clipped to the requested interval.
    AFK intervals are excluded.
    """

    window_events = get_window_events(start_time, end_time)
    afk_events = get_afk_events(start_time, end_time)

    focused_seconds_by_app = {}

    for event in window_events:
        timestamp = event.get("timestamp")
        if not timestamp:
            continue

        try:
            event_start = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if event_start.tzinfo is None:
            event_start = event_start.astimezone()

        duration = float(event.get("duration", 0))

        if duration <= 0:
            continue

        event_end = event_start + timedelta(seconds=duration)

        clipped_start = max(event_start, start_time)
        clipped_end = min(event_end, end_time)

        overlap_seconds = (
            clipped_end - clipped_start
        ).total_seconds()

        if overlap_seconds <= 0:
            continue

        data = event.get("data", {})

        app_name = (
            data.get("app")
            or data.get("application")
            or "Unknown"
        )

        focused_seconds_by_app[app_name] = (
            focused_seconds_by_app.get(app_name, 0)
            + overlap_seconds
        )


    afk_intervals = []

    for event in afk_events:
        timestamp = event.get("timestamp")
        if not timestamp:
            continue

        try:
            event_start = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if event_start.tzinfo is None:
            event_start = event_start.astimezone()

        duration = max(0, float(event.get("duration", 0)))

        event_end = event_start + timedelta(seconds=duration)

        data = event.get("data", {})
        status = str(data.get("status", "")).lower()

        # ActivityWatch commonly uses 1 for AFK and 0 for active.
        is_afk = status in ("1", "afk", "true")

        if is_afk and duration > 0:
            afk_intervals.append((event_start, event_end))

    
    for app_name in list(focused_seconds_by_app):
        focused_seconds_by_app[app_name] = max(
            0, focused_seconds_by_app[app_name]
        )


    total_afk_seconds = 0

    for afk_start, afk_end in afk_intervals:
        clipped_start = max(afk_start, start_time)
        clipped_end = min(afk_end, end_time)

        total_afk_seconds += max(
            0,
            (clipped_end - clipped_start).total_seconds()
        )

    total_window_seconds = sum(focused_seconds_by_app.values())

    if total_window_seconds > 0 and total_afk_seconds > 0:
        # Limit subtraction to avoid negative application durations.
        adjustment = min(total_afk_seconds, total_window_seconds)

        for app_name in focused_seconds_by_app:
            proportion = (
                focused_seconds_by_app[app_name]
                / total_window_seconds
            )

            focused_seconds_by_app[app_name] = max(
                0,
                focused_seconds_by_app[app_name]
                - adjustment * proportion
            )

    focused_minutes_by_app = {
        app_name: round(seconds / 60, 2)
        for app_name, seconds in focused_seconds_by_app.items()
        if seconds > 0
    }

    return {
        "focused_minutes_by_application": focused_minutes_by_app,
        "window_events_fetched": len(window_events),
        "afk_events_fetched": len(afk_events)
    }


def save_fallback(record, error_message):
    """Save failed Firebase records locally."""

    fallback_record = {
        "record": record,
        "error": str(error_message),
        "saved_at": datetime.now().astimezone().isoformat()
    }

    with open(FALLBACK_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(fallback_record) + "\n")

    logging.error("Saved failed record to local fallback file.")



def upload_record(record, max_attempts=5):
    """Upload one record using exponential backoff."""

    document_id = (
        f"{record['date']}_"
        f"{record['hour']:02d}_"
        f"{record['app'].replace('/', '_')}"
    )

    for attempt in range(max_attempts):
        try:
            db.collection(COLLECTION_NAME).document(
                document_id
            ).set(record)

            logging.info(
                "Uploaded %s: %.2f minutes",
                record["app"],
                record["duration_minutes"]
            )

            print(
                f"Uploaded {record['app']}: "
                f"{record['duration_minutes']} minutes"
            )

            return True

        except Exception as error:
            logging.exception(
                "Firebase upload attempt %s failed.",
                attempt + 1
            )

            if attempt < max_attempts - 1:
                delay = 2 ** attempt

                print(
                    f"Upload failed. Retrying in {delay} seconds..."
                )

                time.sleep(delay)
            else:
                save_fallback(record, error)

                print(
                    f"Upload failed after {max_attempts} attempts. "
                    "Record saved locally."
                )

    return False




def sync_focus_metrics():
    """Calculate and upload metrics for the most recently completed hour."""

    print("\nStarting hourly focus metrics sync...")

    # Use the previous fully completed local-clock hour.
    now = datetime.now().astimezone()

    current_hour_start = now.replace(
        minute=0,
        second=0,
        microsecond=0
    )

    end_time = current_hour_start
    start_time = end_time - timedelta(hours=1)

    try:
        results = calculate_hourly_focus(start_time, end_time)

        metrics = results["focused_minutes_by_application"]

        if not metrics:
            print("No application focus metrics found for this hour.")
            logging.info("No application focus metrics found.")
            return

        for app_name, duration_minutes in metrics.items():
            record = {
                "date": start_time.date().isoformat(),
                "hour": start_time.hour,
                "app": app_name,
                "duration_minutes": duration_minutes,
                "source_period_start": start_time.isoformat(),
                "source_period_end": end_time.isoformat(),
                "uploaded_at": datetime.now().astimezone().isoformat(),
                "window_events_fetched": results[
                    "window_events_fetched"
                ],
                "afk_events_fetched": results[
                    "afk_events_fetched"
                ]
            }

            upload_record(record)

        print("Hourly focus metrics sync finished.")

    except Exception as error:
        logging.exception("Focus metrics sync failed.")

        print("Focus metrics sync failed:", error)


if __name__ == "__main__":
    print("Day 13 Hourly Focus Metrics Sync")
    print("Connecting to ActivityWatch and Firebase...")

    # Run once immediately.
    sync_focus_metrics()

    # Run again every hour.
    schedule.every(1).hour.do(sync_focus_metrics)

    print("\nHourly scheduler is running.")
    print("Keep this terminal open to continue syncing.")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nScheduler stopped.")
        logging.info("Scheduler stopped by user.")