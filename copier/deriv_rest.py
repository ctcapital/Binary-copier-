"""Deriv's current REST API (api.derivws.com), used by `pat_` tokens.

The legacy WebSocket API authenticates with an `a1-` token and an `authorize`
message. The current platform instead takes a Personal Access Token as an HTTP
Bearer credential, and a WebSocket is opened by first exchanging that token for
a short-lived OTP:

    GET  /trading/v1/options/accounts                -> account ids
    POST /trading/v1/options/accounts/{id}/otp       -> {"url": "wss://...?otp=..."}
    connect to that url                              (no authorize call)

The OTP is valid for 120 seconds and is single use, so the socket must be
opened straight after the exchange.
"""

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE_URL = "https://api.derivws.com"
TIMEOUT = 30


class RestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__("HTTP {} {}: {}".format(status, code, message))
        self.status = status
        self.code = code
        self.message = message


def _request(
    method: str,
    path: str,
    token: str,
    app_id: str,
) -> Tuple[int, Dict[str, Any]]:
    """Return (status, parsed body). Never raises for HTTP error statuses."""
    req = urllib.request.Request(BASE_URL + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Deriv-App-ID", str(app_id))
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise RestError(0, "NetworkError", str(exc.reason))

    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {"raw": raw[:500]}
    return status, body


# Some failures come back as plain text rather than the JSON envelope, so map
# the ones worth acting on to a stable code.
_PLAIN_ERRORS = (
    ("invalid application", "InvalidApplication"),
    ("invalid or expired token", "InvalidToken"),
    ("deriv-app-id header is required", "MissingAppId"),
    ("missing authorization header", "MissingToken"),
)

GUIDANCE = {
    "InvalidApplication":
        "Deriv accepted the token but rejected the app id. The current API "
        "only accepts an app registered on the new platform — legacy ids such "
        "as 1089 do not work. Create one at developers.deriv.com → Registered "
        "apps and paste its numeric id into the app_id field.",
    "InvalidToken":
        "Deriv did not recognise this token. It may have been revoked, expired "
        "(tokens last at most 90 days) or copied incompletely — it is shown "
        "only once, so create a new one if in doubt.",
    "MissingAppId":
        "The Deriv-App-ID header was empty. Put your registered app's numeric "
        "id in the app_id field.",
    "MissingToken":
        "No token was sent. Save your API token on the Connections page.",
}


def _first_error(body: Dict[str, Any]) -> Tuple[str, str]:
    errors = body.get("errors") or []
    if errors:
        return errors[0].get("code", "Unknown"), errors[0].get("message", "")

    raw = str(body.get("raw") or "").strip()
    if raw:
        lowered = raw.lower()
        for needle, code in _PLAIN_ERRORS:
            if needle in lowered:
                return code, raw
        return "Unknown", raw[:200]

    return "Unknown", json.dumps(body)[:200]


def guidance_for(code: str) -> Optional[str]:
    """Plain-English next step for an error code, if we know one."""
    return GUIDANCE.get(code)


def _unwrap(status: int, body: Dict[str, Any]) -> Any:
    if status >= 400:
        code, message = _first_error(body)
        raise RestError(status, code, message)
    # Successful payloads are wrapped in a "data" envelope.
    return body.get("data", body)


def get_accounts(token: str, app_id: str) -> List[Dict[str, Any]]:
    status, body = _request("GET", "/trading/v1/options/accounts", token, app_id)
    data = _unwrap(status, body)
    if isinstance(data, dict):
        for key in ("accounts", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


def migration_status(token: str, app_id: str) -> Dict[str, Any]:
    """Whether this user has been moved off the legacy options platform."""
    status, body = _request(
        "GET", "/trading/v1/options/legacy/migration-status", token, app_id
    )
    data = _unwrap(status, body)
    return data if isinstance(data, dict) else {"status": str(data)}


def get_ws_url(token: str, app_id: str, account_id: str) -> str:
    """Exchange the PAT for a ready-to-use, OTP-bearing WebSocket URL."""
    status, body = _request(
        "POST",
        "/trading/v1/options/accounts/{}/otp".format(account_id),
        token,
        app_id,
    )
    data = _unwrap(status, body)
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        otp = data.get("otp") if isinstance(data, dict) else None
        if not otp:
            raise RestError(status, "NoUrl",
                            "OTP response contained neither url nor otp: "
                            "{}".format(json.dumps(data)[:200]))
        # Fall back to building it ourselves if only the OTP came back.
        kind = "demo" if _looks_demo(account_id) else "real"
        url = "wss://api.derivws.com/trading/v1/options/ws/{}?otp={}".format(kind, otp)
    return url


def _looks_demo(account_id: str) -> bool:
    return str(account_id).upper().startswith(("VR", "DEMO", "DOT"))


def is_pat(token: str) -> bool:
    return (token or "").strip().lower().startswith("pat_")


def app_id_warning(app_id: str) -> Optional[str]:
    """Flag an app_id that clearly isn't one.

    Two shapes are legitimate: legacy numeric ids (1089), and the current
    platform's alphanumeric ids (e.g. k7Jd2Xq8mNpR4vTwYzB6c). Anything short
    and non-numeric is almost always a name pasted into the wrong field.
    """
    value = (app_id or "").strip()
    if not value:
        return "The app_id is required. Register an app at " \
               "developers.deriv.com → Registered apps."
    if value.isdigit():
        return None
    if len(value) >= 12 and all(c.isalnum() or c in "-_" for c in value):
        return None
    return (
        "`{}` doesn't look like an app id. It's either a number (legacy apps) "
        "or a long alphanumeric string such as `k7Jd2Xq8mNpR4vTwYzB6c`. Check "
        "you copied the app's **id** and not its name.".format(value)
    )
