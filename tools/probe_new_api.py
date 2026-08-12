"""Probe Deriv's current (PAT) API with the token stored in the app.

Run this and paste the whole output back — it never prints your token, and it
places no trades. It answers three things:

  1. Does the PAT authenticate against api.derivws.com at all?
  2. Which Options accounts exist, and are they demo or real?
  3. Does the OTP -> WebSocket handshake work, and what does the socket say?

    ./.venv/bin/python tools/probe_new_api.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets  # noqa: E402

from copier import config, db, deriv_rest  # noqa: E402
from copier.deriv_rest import RestError  # noqa: E402

REDACT_KEYS = ("otp", "token", "url")


def safe(obj, depth=0):
    """Render a payload without leaking the OTP or any URL containing it."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.lower() in REDACT_KEYS and isinstance(v, str):
                out[k] = "<redacted {} chars>".format(len(v))
            else:
                out[k] = safe(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [safe(v, depth + 1) for v in obj[:10]]
    return obj


async def listen(url: str, seconds: float = 8.0):
    """Open the OTP WebSocket and report whatever it sends unprompted."""
    try:
        async with websockets.connect(url, ping_interval=None, open_timeout=20) as ws:
            print("    WebSocket: CONNECTED")
            got = []
            try:
                while len(got) < 5:
                    raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
                    got.append(raw[:400])
            except asyncio.TimeoutError:
                pass
            if got:
                print("    Unprompted messages:")
                for message in got:
                    print("      " + message)
            else:
                print("    (socket stayed silent — it likely waits for a request)")

            # Probe whether the legacy message vocabulary still applies.
            for probe in ({"ping": 1}, {"active_symbols": "brief"}):
                try:
                    await ws.send(json.dumps(probe))
                    reply = await asyncio.wait_for(ws.recv(), timeout=6)
                    print("    {} -> {}".format(json.dumps(probe), reply[:300]))
                except asyncio.TimeoutError:
                    print("    {} -> (no reply)".format(json.dumps(probe)))
                except Exception as exc:
                    print("    {} -> {}: {}".format(json.dumps(probe),
                                                    type(exc).__name__, exc))
    except Exception as exc:
        print("    WebSocket FAILED: {}: {}".format(type(exc).__name__, str(exc)[:200]))


async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file",
                        help="read the token from this file instead of the DB")
    parser.add_argument("--app-id", help="override the stored Deriv-App-ID")
    args = parser.parse_args()

    db.init()
    cfg = config.load()
    if args.token_file:
        with open(args.token_file) as handle:
            token = handle.read().strip()
    else:
        token = (cfg.deriv_token or "").strip()
    app_id = args.app_id or cfg.deriv_app_id or "1089"

    print("=" * 66)
    print("Deriv current-API (PAT) probe")
    print("=" * 66)
    if not token:
        print("No token stored. Save it on the Connections page first.")
        return 1
    print("Token prefix:  {}...  ({} chars)".format(token[:4], len(token)))
    print("Deriv-App-ID:  {}".format(app_id))
    print("-" * 66)

    print("[1] GET /trading/v1/options/legacy/migration-status")
    try:
        print("    -> {}".format(json.dumps(safe(
            deriv_rest.migration_status(token, app_id)))))
    except RestError as exc:
        print("    -> FAILED {}".format(exc))

    print("[2] GET /trading/v1/options/accounts")
    accounts = []
    try:
        accounts = deriv_rest.get_accounts(token, app_id)
        print("    -> {} account(s): {}".format(
            len(accounts), json.dumps(safe(accounts))))
    except RestError as exc:
        print("    -> FAILED {}".format(exc))
        if exc.status == 401:
            print("       401 means the token or the Deriv-App-ID was rejected.")
            print("       Register an app at developers.deriv.com -> Registered")
            print("       apps and put its id in the app_id field.")
        return 1

    if not accounts:
        print("    No Options accounts on this login — create one in Deriv first.")
        return 1

    print("[3] POST /trading/v1/options/accounts/{id}/otp  +  WebSocket")
    for account in accounts[:2]:
        account_id = (account.get("id") or account.get("account_id")
                      or account.get("loginid") or "")
        if not account_id:
            print("    (account had no recognisable id: {})".format(
                json.dumps(safe(account))))
            continue
        print("  account {}:".format(account_id))
        try:
            url = deriv_rest.get_ws_url(token, app_id, account_id)
            print("    OTP exchange OK -> {}".format(
                url.split("?")[0] + "?otp=<redacted>"))
            await listen(url)
        except RestError as exc:
            print("    OTP FAILED {}".format(exc))

    print("-" * 66)
    print("Paste this whole output back. Nothing above reveals your token.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
