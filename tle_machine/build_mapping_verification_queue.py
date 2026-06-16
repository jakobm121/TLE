from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_API_DIR = Path("data/source/api_tennis")
MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")
OVERRIDES_JSON = Path("data/metadata/api_tennis/player_mapping_overrides.json")
REVIEW_CSV = Path("data/reports/api_tennis/player_mapping_review.csv")
CANDIDATES_CSV = Path("data/reports/api_tennis/player_mapping_sackmann_candidate_search.csv")
REPORT_DIR = Path("data/reports/api_tennis")
QUEUE_CSV = REPORT_DIR / "player_mapping_manual_verification_queue.csv"
QUEUE_JSON = REPORT_DIR / "player_mapping_manual_verification_queue.json"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text)


def lower_text(value: Any) -> str:
    return norm_text(value).lower()


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def player_key_variants(api_key: str) -> list[str]:
    api_key = norm_text(api_key)
    out = [api_key]
    m = re.fullmatch(r"(men|women):api_tennis:(.+)", api_key)
    if m:
        out.append(f"{m.group(1)}:api:{m.group(2)}")
    m = re.fullmatch(r"(men|women):api:(.+)", api_key)
    if m:
        out.append(f"{m.group(1)}:api_tennis:{m.group(2)}")
    return list(dict.fromkeys(out))


def api_player_key(gender: str, api_id: str, name: str) -> str:
    if api_id:
        return f"{gender}:api:{api_id}"
    # Should be rare, but keep stable fallback.
    s = lower_text(name)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "unknown"
    return f"{gender}:api_name:{s}"


def load_mapping_statuses() -> dict[str, str]:
    statuses: dict[str, str] = {}
    data = read_json(MAPPING_JSON) or {}
    if isinstance(data, dict):
        for key, value in data.items():
            status = ""
            if isinstance(value, dict):
                status = norm_text(value.get("status"))
            elif value:
                status = "mapped"
            if status:
                statuses[key] = status
    overrides = read_json(OVERRIDES_JSON) or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if value:
                statuses[key] = "manual_mapped"
            else:
                statuses[key] = "manual_unmapped"
    return statuses


def is_mapped(api_key: str, statuses: dict[str, str]) -> bool:
    for key in player_key_variants(api_key):
        status = statuses.get(key)
        if status in {"auto_mapped", "manual_mapped", "mapped"}:
            return True
    return False


def get_side_player(match: dict[str, Any], side: str) -> tuple[str, str, str]:
    obj = match.get(side) or {}
    if not isinstance(obj, dict):
        return "", "", ""
    name = norm_text(obj.get("name"))
    api_id = norm_text(obj.get("api_player_id"))
    pkey = norm_text(obj.get("player_key")) or api_player_key(norm_text(match.get("gender")), api_id, name)
    return pkey, api_id, name


def collect_unresolved_from_review() -> dict[str, dict[str, Any]]:
    unresolved: dict[str, dict[str, Any]] = {}
    for row in read_csv(REVIEW_CSV):
        key = norm_text(row.get("api_player_key") or row.get("api_key") or row.get("player_key"))
        if not key:
            # Try to reconstruct if columns exist.
            gender = norm_text(row.get("gender"))
            api_id = norm_text(row.get("api_player_id") or row.get("api_id"))
            if gender and api_id:
                key = f"{gender}:api:{api_id}"
        if not key:
            continue
        status = norm_text(row.get("status")) or norm_text(row.get("mapping_status"))
        if status and status not in {"ambiguous", "unmapped", "missing_mapping_entry", "manual_review"}:
            continue
        unresolved[key] = {
            "api_player_key": key,
            "api_player_id": norm_text(row.get("api_player_id") or row.get("api_id")) or key.split(":")[-1],
            "api_name": norm_text(row.get("api_name") or row.get("player_name") or row.get("name")),
            "gender": norm_text(row.get("gender")) or key.split(":", 1)[0],
            "mapping_status": status or "unresolved",
            "review_match_count": norm_text(row.get("match_count")),
        }
    return unresolved


def collect_candidates() -> dict[str, list[dict[str, str]]]:
    by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(CANDIDATES_CSV):
        key = norm_text(row.get("api_player_key") or row.get("api_key") or row.get("player_key"))
        if not key:
            gender = norm_text(row.get("gender"))
            api_id = norm_text(row.get("api_player_id") or row.get("api_id"))
            if gender and api_id:
                key = f"{gender}:api:{api_id}"
        if key:
            by_key[key].append(row)
    return by_key


def compact_candidates(rows: list[dict[str, str]], limit: int = 5) -> str:
    parts: list[str] = []
    for r in rows[:limit]:
        sk = norm_text(r.get("candidate_player_key") or r.get("sackmann_player_key") or r.get("best_candidate_key"))
        sn = norm_text(r.get("candidate_name") or r.get("sackmann_name") or r.get("best_candidate_name"))
        score = norm_text(r.get("score") or r.get("best_score"))
        action = norm_text(r.get("suggested_action") or r.get("status"))
        if sk or sn:
            parts.append(" | ".join(x for x in [sk, sn, f"score={score}" if score else "", action] if x))
    return " ; ".join(parts)


def candidate_action(rows: list[dict[str, str]]) -> str:
    for r in rows:
        action = lower_text(r.get("suggested_action") or r.get("status"))
        if action:
            return action
    return ""


def collect_api_matches() -> dict[str, list[dict[str, Any]]]:
    matches_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(SOURCE_API_DIR.glob("tle_api_matches_*.jsonl.gz")):
        for match in read_jsonl_gz(path):
            gender = norm_text(match.get("gender"))
            level = norm_text(match.get("level"))
            surface = norm_text(match.get("surface"))
            date = norm_text(match.get("date"))
            tournament = norm_text(match.get("tourney_name") or match.get("league_name"))
            score = norm_text(match.get("score"))
            round_name = norm_text(match.get("round"))
            event_type = norm_text(match.get("event_type_type"))
            status = norm_text(match.get("event_status"))
            source_file = norm_text(match.get("source_file"))
            api_event_key = norm_text(match.get("api_event_key"))
            winner_key, winner_id, winner_name = get_side_player(match, "winner")
            loser_key, loser_id, loser_name = get_side_player(match, "loser")
            if winner_key:
                matches_by_player[winner_key].append({
                    "result_side": "winner",
                    "api_player_id": winner_id,
                    "api_name": winner_name,
                    "opponent_key": loser_key,
                    "opponent_api_id": loser_id,
                    "opponent_name": loser_name,
                    "date": date,
                    "tournament": tournament,
                    "round": round_name,
                    "level": level,
                    "surface": surface,
                    "score": score,
                    "gender": gender,
                    "event_type_type": event_type,
                    "event_status": status,
                    "source_file": source_file,
                    "api_event_key": api_event_key,
                })
            if loser_key:
                matches_by_player[loser_key].append({
                    "result_side": "loser",
                    "api_player_id": loser_id,
                    "api_name": loser_name,
                    "opponent_key": winner_key,
                    "opponent_api_id": winner_id,
                    "opponent_name": winner_name,
                    "date": date,
                    "tournament": tournament,
                    "round": round_name,
                    "level": level,
                    "surface": surface,
                    "score": score,
                    "gender": gender,
                    "event_type_type": event_type,
                    "event_status": status,
                    "source_file": source_file,
                    "api_event_key": api_event_key,
                })
    return matches_by_player


def priority_for(player: dict[str, Any], matches: list[dict[str, Any]], action: str) -> tuple[int, str]:
    levels = Counter(m.get("level", "") for m in matches)
    if action == "safe_override":
        return 0, "safe_candidate"
    if levels.get("atp_wta") or levels.get("grand_slam"):
        return 1, "high_level"
    if levels.get("challenger"):
        return 2, "challenger"
    if len(matches) >= 5:
        return 3, "many_matches"
    return 4, "low_priority"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build manual verification queue for unresolved API player mappings.")
    parser.add_argument("--examples-per-player", type=int, default=3)
    args = parser.parse_args(argv)

    unresolved = collect_unresolved_from_review()
    candidates = collect_candidates()
    statuses = load_mapping_statuses()
    matches_by_player = collect_api_matches()

    rows: list[dict[str, Any]] = []
    counters = Counter()

    for api_key, player in unresolved.items():
        if is_mapped(api_key, statuses):
            counters["skipped_already_mapped"] += 1
            continue
        matches = []
        for key in player_key_variants(api_key):
            matches.extend(matches_by_player.get(key, []))
        # Deduplicate examples by event key/date/opponent.
        seen = set()
        unique_matches = []
        for m in sorted(matches, key=lambda x: (x.get("date", ""), x.get("level", ""), x.get("tournament", "")), reverse=True):
            mk = (m.get("api_event_key"), m.get("date"), m.get("opponent_key"))
            if mk in seen:
                continue
            seen.add(mk)
            unique_matches.append(m)

        cand_rows = candidates.get(api_key, [])
        action = candidate_action(cand_rows)
        pri_num, pri_label = priority_for(player, unique_matches, action)
        counters["players_in_queue"] += 1
        counters[f"priority_{pri_label}"] += 1
        counters[f"candidate_action_{action or 'none'}"] += 1

        if not unique_matches:
            rows.append({
                "priority_rank": pri_num,
                "priority": pri_label,
                "api_player_key": api_key,
                "api_player_id": player.get("api_player_id", ""),
                "api_name": player.get("api_name", ""),
                "gender": player.get("gender", ""),
                "mapping_status": player.get("mapping_status", ""),
                "api_source_match_count": 0,
                "example_index": 0,
                "date": "",
                "tournament": "",
                "level": "",
                "surface": "",
                "round": "",
                "result_side": "",
                "opponent_key": "",
                "opponent_name": "",
                "opponent_mapped": "",
                "score": "",
                "api_event_key": "",
                "source_file": "",
                "candidate_action": action,
                "candidate_count": len(cand_rows),
                "top_candidates": compact_candidates(cand_rows),
                "search_query_hint": f"{player.get('api_name','')} tennis results",
            })
            continue

        for idx, m in enumerate(unique_matches[: max(args.examples_per_player, 1)], start=1):
            opponent_mapped = is_mapped(norm_text(m.get("opponent_key")), statuses)
            rows.append({
                "priority_rank": pri_num,
                "priority": pri_label,
                "api_player_key": api_key,
                "api_player_id": player.get("api_player_id", "") or m.get("api_player_id", ""),
                "api_name": player.get("api_name", "") or m.get("api_name", ""),
                "gender": player.get("gender", "") or m.get("gender", ""),
                "mapping_status": player.get("mapping_status", ""),
                "api_source_match_count": len(unique_matches),
                "example_index": idx,
                "date": m.get("date", ""),
                "tournament": m.get("tournament", ""),
                "level": m.get("level", ""),
                "surface": m.get("surface", ""),
                "round": m.get("round", ""),
                "result_side": m.get("result_side", ""),
                "opponent_key": m.get("opponent_key", ""),
                "opponent_name": m.get("opponent_name", ""),
                "opponent_mapped": "yes" if opponent_mapped else "no",
                "score": m.get("score", ""),
                "api_event_key": m.get("api_event_key", ""),
                "source_file": m.get("source_file", ""),
                "candidate_action": action,
                "candidate_count": len(cand_rows),
                "top_candidates": compact_candidates(cand_rows),
                "search_query_hint": " ".join(x for x in [m.get("api_name") or player.get("api_name", ""), m.get("opponent_name", ""), m.get("tournament", ""), m.get("date", ""), m.get("score", "")] if x),
            })

    rows.sort(key=lambda r: (int(r.get("priority_rank", 99)), r.get("api_player_key", ""), int(r.get("example_index", 0))))
    fieldnames = [
        "priority_rank", "priority", "api_player_key", "api_player_id", "api_name", "gender", "mapping_status",
        "api_source_match_count", "example_index", "date", "tournament", "level", "surface", "round", "result_side",
        "opponent_key", "opponent_name", "opponent_mapped", "score", "api_event_key", "source_file",
        "candidate_action", "candidate_count", "top_candidates", "search_query_hint",
    ]
    write_csv(QUEUE_CSV, rows, fieldnames)
    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "unresolved_players_from_review": len(unresolved),
        "rows_written": len(rows),
        "outputs": {"queue_csv": str(QUEUE_CSV), "queue_json": str(QUEUE_JSON)},
        "counters": dict(counters),
        "notes": [
            "Use search_query_hint to verify exact match online before adding manual overrides.",
            "Only add overrides when the full player identity and Sackmann key are both confirmed.",
        ],
    }
    write_json(QUEUE_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
