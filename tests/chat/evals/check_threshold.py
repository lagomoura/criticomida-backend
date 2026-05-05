"""Enforce a pass-rate threshold from a pytest JUnit XML report.

Usage::

    python tests/chat/evals/check_threshold.py <report.xml> <threshold>

The threshold is a fraction in ``[0, 1]`` (e.g. ``0.9`` for 90%).
Exits 0 when the rate is at or above the threshold, non-zero otherwise.

The CI workflow invokes this so the chat eval suite can ship as a
gate without failing the job on the first flaky case. We keep it
runnable as a one-off too — useful when triaging a regression
locally without re-running the full suite.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: check_threshold.py <report.xml> <threshold>",
            file=sys.stderr,
        )
        return 2

    report_path, raw_threshold = argv[1], argv[2]
    try:
        threshold = float(raw_threshold)
    except ValueError:
        print(f"invalid threshold: {raw_threshold!r}", file=sys.stderr)
        return 2

    try:
        root = ET.parse(report_path).getroot()
    except (FileNotFoundError, ET.ParseError) as exc:
        print(f"could not read report at {report_path}: {exc}", file=sys.stderr)
        return 2

    suites = root.findall("testsuite") or [root]
    total = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)

    ran = total - skipped
    if ran <= 0:
        print(
            "no tests ran (everything skipped) — treating as failure",
            file=sys.stderr,
        )
        return 1

    passed = ran - failures - errors
    rate = passed / ran
    print(
        f"chat-evals: {passed}/{ran} passed ({rate * 100:.1f}%); "
        f"threshold {threshold * 100:.0f}%"
    )
    if rate + 1e-9 < threshold:
        print(
            f"FAIL: pass-rate {rate * 100:.1f}% is below threshold "
            f"{threshold * 100:.0f}%",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
