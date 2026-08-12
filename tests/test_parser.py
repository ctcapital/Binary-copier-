"""Parser tests. Run: ./.venv/bin/python -m pytest tests/ -q  (or run this file directly)"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copier.parser import ParseError, Signal, parse_signal  # noqa: E402

GOOD = [
    ("AUDCHF 15 minutes Down",            Signal("AUDCHF", "PUT",  15, "m")),
    ("EURGBP 15 minutes Up",              Signal("EURGBP", "CALL", 15, "m")),
    ("AUD/CHF 15 min UP",                 Signal("AUDCHF", "CALL", 15, "m")),
    ("EUR-USD 5m DOWN",                   Signal("EURUSD", "PUT",   5, "m")),
    ("🔴 EURUSD 15 minutes",              Signal("EURUSD", "PUT",  15, "m")),
    ("🟢 GBPJPY 1 hour",                  Signal("GBPJPY", "CALL",  1, "h")),
    ("USDJPY 5 ticks call",               Signal("USDJPY", "CALL",  5, "t")),
    ("SIGNAL\nNZDCAD\n15 minutes\nFALL",  Signal("NZDCAD", "PUT",  15, "m")),
    ("XAUUSD 30 MINUTES RISE",            Signal("XAUUSD", "CALL", 30, "m")),
    ("R_100 15 minutes up",               Signal("R_100",  "CALL", 15, "m")),
    ("frxEURGBP 15 minutes down",         Signal("frxEURGBP", "PUT", 15, "m")),
    # No expiry stated -> falls back to the configured default (15m).
    ("AUDCHF DOWN",                       Signal("AUDCHF", "PUT",  15, "m")),
    # A price level must not be read as a duration.
    ("EURUSD entry 1.0850 15 minutes up", Signal("EURUSD", "CALL", 15, "m")),
]

BAD = [
    "",
    "good morning traders",
    "AUDCHF 15 minutes",              # no direction
    "15 minutes up",                  # no pair
    "AUDCHF 15 minutes up and down",  # contradictory direction
    "results: 8 wins 2 losses today",
]


def main() -> int:
    failures = []

    for text, expected in GOOD:
        try:
            got = parse_signal(text)
        except ParseError as exc:
            failures.append("REJECTED {!r}: {}".format(text, exc))
            continue
        if got != expected:
            failures.append("MISMATCH {!r}\n    expected {}\n    got      {}".format(
                text, expected, got))

    for text in BAD:
        try:
            got = parse_signal(text)
            failures.append("SHOULD REJECT {!r} -> {}".format(text, got))
        except ParseError:
            pass

    total = len(GOOD) + len(BAD)
    if failures:
        print("FAILED {}/{}".format(len(failures), total))
        for failure in failures:
            print("  " + failure)
        return 1

    print("All {} parser cases passed.".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
