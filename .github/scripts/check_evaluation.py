"""Fail CI when the mock evaluation CSV contains a correctness regression."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REQUIRED_COLUMNS = {"expect_matched", "fields_ok", "facts_ok", "degraded"}


def check_report(path: Path) -> list[str]:
    problems: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            return [f"Missing CSV columns: {', '.join(sorted(missing))}"]

        count = 0
        for count, row in enumerate(reader, start=1):
            label = row.get("id") or f"row {count}"
            for column, accepted in (
                ("expect_matched", {"True"}),
                ("fields_ok", {"True", ""}),
                ("facts_ok", {"True"}),
                ("degraded", {"False"}),
            ):
                if row.get(column) not in accepted:
                    problems.append(f"{label}: {column}={row.get(column)!r}")
        if count == 0:
            problems.append("The evaluation CSV contains no samples.")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="CSV produced by scripts/evaluate.py")
    args = parser.parse_args()
    try:
        problems = check_report(args.report)
    except (OSError, UnicodeError, csv.Error) as exc:
        print(f"Cannot read evaluation report: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("Evaluation regression check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("Evaluation regression check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
