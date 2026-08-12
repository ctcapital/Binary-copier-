"""Diagnose a Deriv token rejection.

Tests the token exactly as the app has it stored, and reports what Deriv says.
The token itself is never printed in full.

    ./.venv/bin/python tools/check_deriv.py              # test the stored token
    ./.venv/bin/python tools/check_deriv.py --token XXX  # test one you paste in
"""

import argparse
import asyncio
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copier import config, db  # noqa: E402
from copier.deriv import DerivClient, DerivError, token_warning  # noqa: E402

import websockets  # noqa: E402

ENDPOINTS = [
    ("deriv", "wss://ws.derivws.com/websockets/v3"),
    ("binary", "wss://ws.binaryws.com/websockets/v3"),
]


def mask(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return "{}...{} ({} chars)".format(token[:4], token[-3:], len(token))


def inspect(token: str) -> None:
    print("Token:            {}".format(mask(token)))
    if not token:
        print("  !! Nothing is stored. Save the token on the Connections page first.")
        return

    if token != token.strip():
        print("  !! Has leading/trailing whitespace.")
    weird = [
        (i, ch) for i, ch in enumerate(token)
        if not (ch.isalnum() or ch in "-_")
    ]
    if weird:
        print("  !! Contains {} unexpected character(s): {}".format(
            len(weird),
            ", ".join("pos {} = {!r} ({})".format(
                i, ch, unicodedata.name(ch, "unknown")) for i, ch in weird[:5]),
        ))
    else:
        print("  Charset:        ok (letters, digits, - and _ only)")

    shape = token_warning(token)
    if shape:
        print("  !! {}".format(shape))
        return

    # Deriv tokens are typically 15-40 chars. Far outside that means a bad copy.
    if len(token) < 10:
        print("  !! Only {} characters — this looks truncated. Deriv tokens are "
              "usually 15-40.".format(len(token)))
    elif len(token) > 60:
        print("  !! {} characters — longer than a Deriv token. Did an extra "
              "value get pasted in?".format(len(token)))
    else:
        print("  Length:         {} chars (plausible)".format(len(token)))


async def probe(token: str, app_id: str, endpoint: str):
    client = DerivClient(token=token, app_id=app_id, endpoint=endpoint)
    try:
        client._ws = await websockets.connect(client.url, ping_interval=None)
        client._reader_task = asyncio.ensure_future(client._read_loop())
        data = await client.send({"authorize": token}, timeout=20)
        auth = data.get("authorize", {})
        return True, auth
    except DerivError as exc:
        return False, exc
    except Exception as exc:
        return False, exc
    finally:
        await client._teardown()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", help="test this token instead of the stored one")
    parser.add_argument("--app-id", help="override the stored app_id")
    args = parser.parse_args()

    db.init()
    cfg = config.load()
    token = (args.token or cfg.deriv_token or "").strip()
    app_id = args.app_id or cfg.deriv_app_id or "1089"

    print("=" * 62)
    print("Deriv token diagnostic")
    print("=" * 62)
    if os.environ.get("DERIV_TOKEN"):
        print("NOTE: the DERIV_TOKEN environment variable is set and overrides")
        print("      whatever you typed into the app.")
    print("Source:           {}".format(
        "--token argument" if args.token else "stored in data/copier.db"))
    print("app_id:           {}".format(app_id))
    inspect(token)
    print("-" * 62)

    if not token:
        return 1

    ok = False
    for name, endpoint in ENDPOINTS:
        good, result = await probe(token, app_id, endpoint)
        if good:
            ok = True
            print("{:<8} AUTHORIZED".format(name))
            print("         account:  {}".format(result.get("loginid")))
            print("         type:     {}".format(
                "DEMO" if result.get("is_virtual") else "REAL MONEY"))
            print("         currency: {}".format(result.get("currency")))
            print("         balance:  {}".format(result.get("balance")))
            scopes = result.get("scopes") or []
            print("         scopes:   {}".format(", ".join(scopes) or "none"))
            missing = [s for s in ("read", "trade") if s not in scopes]
            if missing:
                print("         !! missing scope(s): {} — the token cannot "
                      "place trades.".format(", ".join(missing)))
        else:
            code = getattr(result, "code", type(result).__name__)
            print("{:<8} rejected: {}".format(name, code))

    print("-" * 62)
    if ok:
        print("The token works. If the app still complains, press Save on the")
        print("Connections page so the stored copy matches what you tested.")
        return 0

    print("Deriv rejected this token on every endpoint. That error only comes")
    print("back when the token reached Deriv and did not match a live token,")
    print("so the transport and app_id are not the problem. Check, in order:")
    print("  1. Copy it with the copy button on Deriv's API token page — the")
    print("     displayed value is truncated, so selecting it by hand gives a")
    print("     short, invalid string.")
    print("  2. Confirm the token still exists and was not deleted or")
    print("     regenerated (regenerating invalidates the old value).")
    print("  3. Make sure you created it on the same Deriv account you intend")
    print("     to trade, and on deriv.com rather than a regional mirror.")
    print("  4. Re-create it with the Read, Trade and Trading information")
    print("     scopes ticked, then paste the new value.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
