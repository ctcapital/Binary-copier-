"""Turn a Telegram message into a structured trade signal.

Handles the house format ("AUDCHF 15 minutes Down") plus the usual variations:
separators, emoji direction markers, abbreviated units and extra chatter lines.

Anything ambiguous is rejected rather than guessed at — a wrong direction costs
real money, so "no trade" is always the safer failure.
"""

import re
from typing import NamedTuple, Optional

# Currency codes we accept either side of a pair. Deriv also lists metals
# (XAU/XAG) as forex-style symbols, so they belong here too.
CURRENCIES = {
    "AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "JPY", "MXN", "NOK", "NZD",
    "PLN", "SEK", "SGD", "USD", "ZAR", "XAU", "XAG", "XPD", "XPT",
}

UP_WORDS = {
    "UP", "RISE", "RISES", "RISING", "CALL", "BUY", "HIGHER", "HIGH",
    "LONG", "BULL", "BULLISH",
}
DOWN_WORDS = {
    "DOWN", "FALL", "FALLS", "FALLING", "PUT", "SELL", "LOWER", "LOW",
    "SHORT", "BEAR", "BEARISH",
}

UP_EMOJI = ("⬆", "🔼", "▲", "🟢", "📈", "☝")
DOWN_EMOJI = ("⬇", "🔽", "▼", "🔴", "📉", "👇")

# Deriv duration units. Longest spellings first so "minutes" wins over "m".
_UNIT_ALIASES = [
    (("TICKS", "TICK"), "t"),
    (("SECONDS", "SECOND", "SECS", "SEC"), "s"),
    (("MINUTES", "MINUTE", "MINS", "MIN"), "m"),
    (("HOURS", "HOUR", "HRS", "HR"), "h"),
    (("DAYS", "DAY"), "d"),
    (("T",), "t"),
    (("S",), "s"),
    (("M",), "m"),
    (("H",), "h"),
    (("D",), "d"),
]

_ALL_UNIT_TOKENS = [tok for toks, _ in _UNIT_ALIASES for tok in toks]
_UNIT_LOOKUP = {tok: unit for toks, unit in _UNIT_ALIASES for tok in toks}

# An integer that is not part of a decimal number (so price levels like
# "1.0850" never get mistaken for a duration), followed by a unit token.
_DURATION_RE = re.compile(
    r"(?<![\d.])(\d{1,4})\s*(" + "|".join(_ALL_UNIT_TOKENS) + r")(?![A-Z0-9])"
)

# Two currency codes, optionally separated by / - _ . or a space.
_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*[/\-_.]?\s*([A-Z]{3})\b")

# Symbols already written in Deriv's own notation. Matched against uppercased
# text, so the lowercase frx/cry prefix is restored when we build the result.
_PREFIXED_RE = re.compile(r"\b(FRX|CRY)([A-Z]{6})\b")
_SYNTHETIC_RE = re.compile(r"\b(?:R_\d{1,3}|\d+HZ\d+V)\b")


class Signal(NamedTuple):
    pair: str            # "AUDCHF", or a raw Deriv symbol like "R_100"
    direction: str       # "CALL" or "PUT"
    duration: int        # e.g. 15
    duration_unit: str   # one of t/s/m/h/d


class ParseError(Exception):
    """The message is not a usable signal. The text explains why."""


def _normalise(text: str) -> str:
    """Uppercase, replace emoji direction markers with words, collapse spaces."""
    out = text.upper()
    for emoji in UP_EMOJI:
        out = out.replace(emoji, " UP ")
    for emoji in DOWN_EMOJI:
        out = out.replace(emoji, " DOWN ")
    # Drop variation selectors and other stray symbols, keep signal characters.
    out = re.sub(r"[^\w/\-_.\s]", " ", out, flags=re.UNICODE)
    return re.sub(r"\s+", " ", out).strip()


def _find_direction(text: str) -> str:
    tokens = set(re.findall(r"[A-Z]+", text))
    up = bool(tokens & UP_WORDS)
    down = bool(tokens & DOWN_WORDS)
    if up and down:
        raise ParseError("message contains both up and down keywords")
    if up:
        return "CALL"
    if down:
        return "PUT"
    raise ParseError("no direction keyword found")


def _find_pair(text: str) -> str:
    prefixed = _PREFIXED_RE.search(text)
    if prefixed:
        return prefixed.group(1).lower() + prefixed.group(2)

    synthetic = _SYNTHETIC_RE.search(text)
    if synthetic:
        return synthetic.group(0)

    for match in _PAIR_RE.finditer(text):
        base, quote = match.group(1), match.group(2)
        if base in CURRENCIES and quote in CURRENCIES and base != quote:
            return base + quote
    raise ParseError("no recognisable currency pair found")


def _find_duration(text: str):
    """Return (duration, unit), or None when the message carries no expiry."""
    for match in _DURATION_RE.finditer(text):
        value = int(match.group(1))
        unit = _UNIT_LOOKUP[match.group(2)]
        if value <= 0:
            continue
        return value, unit
    return None


def parse_signal(
    text: str,
    default_duration: int = 15,
    default_unit: str = "m",
) -> Signal:
    """Parse `text` into a Signal, or raise ParseError.

    When the message states no expiry the defaults are used, which is why the
    caller must keep those aligned with the group's usual contract length.
    """
    if not text or not text.strip():
        raise ParseError("empty message")

    norm = _normalise(text)
    direction = _find_direction(norm)
    pair = _find_pair(norm)

    found = _find_duration(norm)
    if found is None:
        duration, unit = default_duration, default_unit
    else:
        duration, unit = found

    return Signal(pair=pair, direction=direction, duration=duration, duration_unit=unit)


# Result announcements the group posts after a trade closes. These are only
# ever consulted for messages the parser has ALREADY rejected, so a false
# positive here can never cause or suppress a trade — it only changes the
# label, keeping genuine parser failures visible instead of buried.
_RESULT_PHRASES = (
    "POSITIVE SIGNAL", "NEGATIVE SIGNAL",
    "WIN", "LOSS", "LOST", "PROFIT", "ITM", "OTM",
    "RESULT", "TAKE PROFIT", "STOP LOSS",
)


def looks_like_result(text: str) -> bool:
    """True if a rejected message is a trade-result post rather than a miss."""
    upper = (text or "").upper()
    return any(phrase in upper for phrase in _RESULT_PHRASES)


def direction_label(direction: Optional[str]) -> str:
    """Deriv's contract types shown in the platform's own words.

    CALL/PUT is what the API expects and what the database stores; Rise/Fall
    is what Deriv shows a trader, so it's what the UI should say.
    """
    if direction == "CALL":
        return "Rise"
    if direction == "PUT":
        return "Fall"
    return direction or ""


def looks_like_signal(text: str) -> bool:
    """Cheap pre-filter so ordinary group chatter never reaches the parser."""
    try:
        parse_signal(text)
        return True
    except ParseError:
        return False
