from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

RAW_DIR = DATA_DIR / "raw"
RAW_SACKMANN_DIR = RAW_DIR / "sackmann"
RAW_ATP_DIR = RAW_SACKMANN_DIR / "atp"
RAW_WTA_DIR = RAW_SACKMANN_DIR / "wta"

SOURCE_DIR = DATA_DIR / "source"
SOURCE_SACKMANN_DIR = SOURCE_DIR / "sackmann"

CANONICAL_DIR = DATA_DIR / "canonical"
RATINGS_DIR = DATA_DIR / "ratings"
REPORTS_DIR = DATA_DIR / "reports"
SACKMANN_REPORTS_DIR = REPORTS_DIR / "sackmann"
RATINGS_REPORTS_DIR = REPORTS_DIR / "ratings"

START_YEAR = 2023

ATP_BASE_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"

SACKMANN_FILES = {
    "atp_main": {
        "gender": "men",
        "base_url": ATP_BASE_URL,
        "template": "atp_matches_{year}.csv",
        "level_hint": "atp_wta",
        "raw_dir": RAW_ATP_DIR,
    },
    "atp_qual_chall": {
        "gender": "men",
        "base_url": ATP_BASE_URL,
        "template": "atp_matches_qual_chall_{year}.csv",
        "level_hint": None,
        "raw_dir": RAW_ATP_DIR,
    },
    "atp_futures": {
        "gender": "men",
        "base_url": ATP_BASE_URL,
        "template": "atp_matches_futures_{year}.csv",
        "level_hint": "itf",
        "raw_dir": RAW_ATP_DIR,
    },
    "wta_main": {
        "gender": "women",
        "base_url": WTA_BASE_URL,
        "template": "wta_matches_{year}.csv",
        "level_hint": "atp_wta",
        "raw_dir": RAW_WTA_DIR,
    },
    "wta_qual_itf": {
        "gender": "women",
        "base_url": WTA_BASE_URL,
        "template": "wta_matches_qual_itf_{year}.csv",
        "level_hint": None,
        "raw_dir": RAW_WTA_DIR,
    },
}

RATING_INITIAL = 1500.0
RATING_K = 32.0
SURFACES = {"hard", "clay", "grass", "carpet"}
LEVELS = {"grand_slam", "atp_wta", "challenger", "itf", "qualifying"}
