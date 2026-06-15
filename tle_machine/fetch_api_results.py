from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_BASE_URL = "https://api.api-tennis.com/tennis/"
RAW_RESULTS_DIR = Path("data/raw/api_tennis/results")
REPORT_DIR = Path("data/reports/api_tennis")
FETCH_REPORT_PATH = REPORT_DIR / "fetch_api_results_report.json"
DEFAULT_DAYS_BACK = 21
DEFAULT_SLEEP_SECONDS = 0.35


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; use YYYY-MM-DD") from exc


def date_range(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def result_count(payload: Any) -> int:
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        response = payload["response"]
    else:
        response = payload

    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict):
            return len(result)
    return 0


def api_success(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    success = response.get("success")
    if success in (1, "1", True, "true", "True"):
        return True
    # Some APIs return an empty but valid list as success=1. If success is absent,
    # do not guess; keep it as failed so old raw files are protected.
    return False


def safe_public_url(date_start: str, date_stop: str) -> str:
    query = urllib.parse.urlencode(
        {
            "method": "get_fixtures",
            "APIkey": "***",
            "date_start": date_start,
            "date_stop": date_stop,
        }
    )
    return f"{API_BASE_URL}?{query}"


def fetch_fixtures(api_key: str, day: date, timeout: int) -> dict[str, Any]:
    day_s = day.isoformat()
    query = urllib.parse.urlencode(
        {
            "method": "get_fixtures",
            "APIkey": api_key,
            "date_start": day_s,
            "date_stop": day_s,
        }
    )
    url = f"{API_BASE_URL}?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tle-machine/1.0",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON response for {day_s}: {raw[:300]}") from exc

    if not isinstance(decoded, dict):
        raise RuntimeError(f"API returned unexpected JSON type for {day_s}: {type(decoded).__name__}")

    return decoded


def build_raw_payload(day: date, response: dict[str, Any]) -> dict[str, Any]:
    day_s = day.isoformat()
    return {
        "schema_version": 1,
        "source": "api_tennis",
        "method": "get_fixtures",
        "date": day_s,
        "fetched_at": now_utc_iso(),
        "request": {
            "date_start": day_s,
            "date_stop": day_s,
            "url_without_key": safe_public_url(day_s, day_s),
        },
        "response": response,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch raw API-Tennis fixture results into data/raw/api_tennis/results.")
    parser.add_argument("--start-date", type=parse_date, default=None)
    parser.add_argument("--end-date", type=parse_date, default=None)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK, help="Days back including today when start/end are not provided.")
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--fail-on-any-error", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("API_TENNIS_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing API_TENNIS_KEY environment variable / GitHub Actions secret.")

    today = datetime.now(timezone.utc).date()
    if args.start_date and args.end_date:
        start_date = args.start_date
        end_date = args.end_date
    elif args.start_date:
        start_date = args.start_date
        end_date = today
    elif args.end_date:
        end_date = args.end_date
        start_date = end_date - timedelta(days=max(args.days - 1, 0))
    else:
        end_date = today
        start_date = today - timedelta(days=max(args.days - 1, 0))

    if start_date > end_date:
        raise RuntimeError(f"start-date {start_date} is after end-date {end_date}")

    ensure_dir(RAW_RESULTS_DIR)
    ensure_dir(REPORT_DIR)

    counters = Counter()
    day_reports: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, day in enumerate(date_range(start_date, end_date), start=1):
        counters["requested_days"] += 1
        day_s = day.isoformat()
        out_path = RAW_RESULTS_DIR / f"{day_s}.json"
        old_payload = read_json(out_path)
        old_count = result_count(old_payload)

        try:
            response = fetch_fixtures(api_key, day, args.timeout)
            success = api_success(response)
            downloaded_count = result_count(response)

            if not success:
                counters["failed_days"] += 1
                counters["kept_existing_files"] += int(out_path.exists())
                error_text = str(response.get("error") or response.get("message") or "API success flag not true")
                errors.append(f"{day_s}: {error_text}")
                day_reports.append(
                    {
                        "date": day_s,
                        "status": "rejected_api_not_success",
                        "old_count": old_count,
                        "downloaded_count": downloaded_count,
                        "path": str(out_path),
                        "error": error_text,
                    }
                )
            else:
                payload = build_raw_payload(day, response)
                write_json(out_path, payload)
                counters["successful_days"] += 1
                counters["written_files"] += 1
                counters["downloaded_fixtures"] += downloaded_count
                day_reports.append(
                    {
                        "date": day_s,
                        "status": "written",
                        "old_count": old_count,
                        "downloaded_count": downloaded_count,
                        "path": str(out_path),
                    }
                )

        except Exception as exc:
            counters["failed_days"] += 1
            counters["kept_existing_files"] += int(out_path.exists())
            errors.append(f"{day_s}: {exc}")
            day_reports.append(
                {
                    "date": day_s,
                    "status": "failed_exception_kept_existing" if out_path.exists() else "failed_exception_no_existing",
                    "old_count": old_count,
                    "downloaded_count": 0,
                    "path": str(out_path),
                    "error": str(exc),
                }
            )

        if index < counters["requested_days"] or args.sleep_seconds:
            time.sleep(max(args.sleep_seconds, 0.0))

    report = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "method": "get_fixtures",
        "date_start": start_date.isoformat(),
        "date_stop": end_date.isoformat(),
        "raw_results_dir": str(RAW_RESULTS_DIR),
        "counters": dict(counters),
        "errors": errors,
        "days": day_reports,
    }
    write_json(FETCH_REPORT_PATH, report)

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))

    if args.fail_on_any_error and errors:
        sys.exit(1)
    if counters["successful_days"] == 0:
        raise RuntimeError("No API days were fetched successfully. Existing raw files were kept where present.")


if __name__ == "__main__":
    main()
