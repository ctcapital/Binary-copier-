"""Map a signal's pair ("AUDCHF") onto a Deriv symbol ("frxAUDCHF").

Deriv's own `active_symbols` list is the source of truth. It gets cached in the
settings table so the engine can resolve symbols without a round trip, and so
an unknown pair is rejected before we spend money on it.
"""

import re
from typing import Any, Dict, List, Optional

from . import db

CACHE_KEY = "deriv_active_symbols"
CACHE_TS_KEY = "deriv_active_symbols_ts"

CRYPTO_BASES = {"BTC", "ETH", "LTC", "XRP", "BCH", "USDC", "SOL", "DOT", "ADA"}

_NATIVE_RE = re.compile(r"^(?:frx|cry)[A-Z]{6}$|^R_\d{1,3}$|^\d+HZ\d+V$|^WLD[A-Z]{3}$")


def is_native(symbol: str) -> bool:
    """True if the text is already a Deriv symbol rather than a plain pair."""
    return bool(_NATIVE_RE.match(symbol))


def candidates(pair: str) -> List[str]:
    """Deriv symbols worth trying for a parsed pair, best guess first."""
    if is_native(pair):
        return [pair]

    upper = pair.upper()
    out = []
    if len(upper) == 6 and upper[:3] in CRYPTO_BASES:
        out.append("cry" + upper)
        out.append("frx" + upper)
    else:
        out.append("frx" + upper)
        out.append("cry" + upper)
    out.append(upper)
    return out


def save_active_symbols(symbols: List[Dict[str, Any]]) -> None:
    import time

    # The legacy API calls these symbol/display_name; the current one calls
    # them underlying_symbol/underlying_symbol_name. Accept either.
    trimmed = [
        {
            "symbol": s.get("symbol") or s.get("underlying_symbol", ""),
            "display_name": (s.get("display_name")
                             or s.get("underlying_symbol_name", "")),
            "market": s.get("market", ""),
            "exchange_is_open": int(s.get("exchange_is_open", 0)),
        }
        for s in symbols
    ]
    trimmed = [s for s in trimmed if s["symbol"]]
    db.set_setting(CACHE_KEY, trimmed)
    db.set_setting(CACHE_TS_KEY, time.time())


def load_active_symbols() -> List[Dict[str, Any]]:
    return db.get_setting(CACHE_KEY, []) or []


def resolve(pair: str) -> Optional[str]:
    """Return the Deriv symbol for `pair`, or None if it isn't tradable here.

    With no cached symbol list we fall back to the best guess so a fresh
    install still works; Deriv itself rejects a bad symbol at proposal time.
    """
    known = {s["symbol"] for s in load_active_symbols()}
    options = candidates(pair)
    if not known:
        return options[0]
    for option in options:
        if option in known:
            return option
    return None


def market_open(symbol: str) -> Optional[bool]:
    """True/False from the cached list, or None when we simply don't know."""
    for entry in load_active_symbols():
        if entry["symbol"] == symbol:
            return bool(entry.get("exchange_is_open", 0))
    return None
