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

FINAL_STATUS_WORDS = {
    "finished",
    "finish",
    "ended",
    "complete",
    "completed",
    "closed",
    "cancelled",
    "canceled",
    "retired",
    "walkover",
    "w/o",
    "wo",
    "abandoned",
    "void",
}

NON_FINAL_STATUS_WORDS = {
    "not started",
    "scheduled",
    "pending",
    "postponed",
    "suspended",
    "interrupted",
    "live",
    "in progress",
    "set",
    "rain",
    "delayed",
    "unknown",
    "",
}


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


def response_result(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        response = payload["response"]
    else:
        response = payload
    if isinstance(response, dict):
        return response.get("result")
    return None


def result_items(payload: Any) -> list[dict[str, Any]]:
    result = response_result(payload)
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        # API-Tennis sometimes returns an object keyed by fixture id.
        return [x for x in result.values() if isinstance(x, dict)]
    return []


def result_count(payload: Any) -> int:
    result = response_result(payload)
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
    # Some APIs return an empty but valid list as success=1.
    # If success is absent, do not guess; keep it as failed so old raw files are protected.
    return False


def normalized_status(match: dict[str, Any]) -> str:
    fields = [
        "event_status",
        "status",
        "match_status",
        "fixture_status",
        "event_status_info",
        "status_info",
    ]
    text = " ".join(str(match.get(k) or "") for k in fields).strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    return " ".join(text.split())


def has_winner_or_final_score(match: dict[str, Any]) -> bool:
    winner_fields = [
        "event_winner",
        "event_winner_key",
        "winner",
        "winner_key",
        "match_winner",
        "match_winner_key",
    ]
    if any(str(match.get(k) or "").strip() for k in winner_fields):
        return True

    score = str(match.get("event_final_result") or match.get("final_score") or "").strip()
    if not score:
        return False
    # Avoid treating an empty placeholder as final.
    return any(ch.isdigit() for ch in score)


def match_is_finalized(match: dict[str, Any]) -> bool:
    status = normalized_status(match)

    if any(word in status for word in NON_FINAL_STATUS_WORDS if word):
        return False

    if any(word in status for word in FINAL_STATUS_WORDS):
        return True

    # API-Tennis commonly uses "Finished" plus winner fields, but keep this fallback
    # for older raw payloads that may have weak status naming.
    if has_winner_or_final_score(match):
        return True

    return False


def payload_is_finalized(payload: Any) -> bool:
    matches = result_items(payload)

    # Empty successful historical day is stable enough to skip after the recent window.
    if not matches:
        return api_success(payload.get("response") if isinstance(payload, dict) and "response" in payload else payload)

    return all(match_is_finalized(match) for match in matches)


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


def should_skip_existing_day(
    *,
    day: date,
    today: date,
    old_payload: Any,
    out_path: Path,
    skip_finalized_existing: bool,
    always_refresh_recent_days: int,
) -> tuple[bool, str]:
    if not skip_finalized_existing:
        return False, "skip_disabled"
    if not out_path.exists() or old_payload is None:
        return False, "missing_existing_file"

    recent_cutoff = today - timedelta(days=max(always_refresh_recent_days - 1, 0))
    if day >= recent_cutoff:
        return False, "inside_recent_refresh_window"

    if payload_is_finalized(old_payload):
        return True, "existing_day_finalized"

    return False, "existing_day_not_finalized"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch raw API-Tennis fixture results into data/raw/api_tennis/results.")
    parser.add_argument("--start-date", type=parse_date, default=None)
    parser.add_argument("--end-date", type=parse_date, default=None)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK, help="Days back including today when start/end are not provided.")
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--fail-on-any-error", action="store_true")
    parser.add_argument(
        "--skip-finalized-existing",
        action="store_true",
        help="Skip API call for existing older daily files where all fixtures are final/cancelled/retired/walkover/abandoned.",
    )
    parser.add_argument(
        "--always-refresh-recent-days",
        type=int,
        default=2,
        help="When --skip-finalized-existing is enabled, still always refetch today plus this many recent days.",
    )
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

    days = list(date_range(start_date, end_date))
    for index, day in enumerate(days, start=1):
        counters["requested_days"] += 1

        day_s = day.isoformat()
        out_path = RAW_RESULTS_DIR / f"{day_s}.json"
        old_payload = read_json(out_path)
        old_count = result_count(old_payload)

        skip, skip_reason = should_skip_existing_day(
            day=day,
            today=today,
            old_payload=old_payload,
            out_path=out_path,
            skip_finalized_existing=args.skip_finalized_existing,
            always_refresh_recent_days=args.always_refresh_recent_days,
        )
        if skip:
            counters["skipped_finalized_existing_days"] += 1
            counters["kept_existing_files"] += 1
            day_reports.append(
                {
                    "date": day_s,
                    "status": "skipped_finalized_existing",
                    "reason": skip_reason,
                    "old_count": old_count,
                    "downloaded_count": 0,
                    "path": str(out_path),
                }
            )
            continue

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
                        "reason": skip_reason,
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
                        "reason": skip_reason,
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
                    "reason": skip_reason,
                    "old_count": old_count,
                    "downloaded_count": 0,
                    "path": str(out_path),
                    "error": str(exc),
                }
            )

        if index < len(days) and args.sleep_seconds:
            time.sleep(max(args.sleep_seconds, 0.0))

    report = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "method": "get_fixtures",
        "date_start": start_date.isoformat(),
        "date_stop": end_date.isoformat(),
        "raw_results_dir": str(RAW_RESULTS_DIR),
        "options": {
            "skip_finalized_existing": bool(args.skip_finalized_existing),
            "always_refresh_recent_days": args.always_refresh_recent_days,
        },
        "counters": dict(counters),
        "errors": errors,
        "days": day_reports,
    }
    write_json(FETCH_REPORT_PATH, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))

    if args.fail_on_any_error and errors:
        sys.exit(1)

    if counters["successful_days"] == 0 and counters["skipped_finalized_existing_days"] == 0:
        raise RuntimeError("No API days were fetched successfully. Existing raw files were kept where present.")


if __name__ == "__main__":
    main()
