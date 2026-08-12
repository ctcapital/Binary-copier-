"""Switch between live and paper trading from the command line.

The dashboard always trades live — there is deliberately no switch in the UI.
Paper mode still exists for dry-running a parser or settings change against
real signals without spending anything:

    ./.venv/bin/python -m copier.mode            # show the current mode
    ./.venv/bin/python -m copier.mode paper      # stop sending orders
    ./.venv/bin/python -m copier.mode live       # resume sending orders

Restart the app afterwards so the running engine picks up the change.
"""

import sys

from . import config, db

VALID = ("live", "paper")


def main(argv) -> int:
    db.init()

    if len(argv) < 2:
        print("mode: {}".format(config.load().mode))
        print("usage: python -m copier.mode [live|paper]")
        return 0

    wanted = argv[1].strip().lower()
    if wanted not in VALID:
        print("error: mode must be one of {}".format(", ".join(VALID)))
        return 1

    before = config.load().mode
    config.save(mode=wanted)

    after = config.load().mode
    if after != wanted:
        print("error: the setting did not persist — is {} writable?".format(db.DB_PATH))
        return 1

    db.log("warning", "cli", "Mode changed from {} to {}".format(before, wanted))
    print("mode: {} -> {}".format(before, after))
    if after == "live":
        print("Live. Every signal now places a real order.")
    else:
        print("Paper. No orders will be sent until you switch back.")
    print("Restart the app for the running engine to pick this up.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
