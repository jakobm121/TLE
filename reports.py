from __future__ import annotations

from .config import CANONICAL_DIR, RATINGS_DIR, SOURCE_SACKMANN_DIR
from .utils import read_json


def main() -> None:
    print("=== Sackmann source ===")
    print(read_json(SOURCE_SACKMANN_DIR / "manifest.json", {}))
    print("=== Canonical ===")
    print(read_json(CANONICAL_DIR / "manifest.json", {}))
    print("=== Ratings ===")
    print(read_json(RATINGS_DIR / "manifest.json", {}))


if __name__ == "__main__":
    main()
