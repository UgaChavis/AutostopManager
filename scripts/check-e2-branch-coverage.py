"""Enforce true branch coverage for the E2 client and its two consumers."""

import argparse
import json
from pathlib import Path
import sys

E2_FILES = (
    "autostop_manager/catalog_clients.py",
    "autostop_manager/vin_oem_resolver.py",
    "autostop_manager/vin_parts_benchmark.py",
)
MINIMUM = 82


def check_report(report):
    if report["meta"]["branch_coverage"] is not True:
        raise ValueError("branch measurement is required")
    covered = total = 0
    for name in E2_FILES:
        summary = report["files"][name]["summary"]
        count, size = summary["covered_branches"], summary["num_branches"]
        if type(count) is not int or type(size) is not int or not 0 <= count <= size or size <= 0:
            raise ValueError(f"invalid branch counts: {name}")
        covered += count
        total += size
    print(f"E2 branch coverage: {covered / total:.2%} ({covered}/{total}); minimum {MINIMUM}%")
    return covered * 100 >= total * MINIMUM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage json report from the complete test suite")
    args = parser.parse_args()
    try:
        passed = check_report(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"E2 branch coverage report invalid: {exc}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
