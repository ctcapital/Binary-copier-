"""Typed access to the settings table, with defaults and env-var overrides.

Secrets can be supplied via environment variables (DERIV_TOKEN, TG_API_ID,
TG_API_HASH) if you would rather not keep them in the database file.
"""

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from . import db

SESSION_DIR = os.path.join(db.BASE_DIR, "session")
SESSION_PATH = os.path.join(SESSION_DIR, "copier")

DEFAULTS: Dict[str, Any] = {
    # Telegram
    "tg_api_id": "",
    "tg_api_hash": "",
    "tg_phone": "",
    "tg_chat_id": "",
    "tg_chat_title": "",
    # Deriv
    "deriv_token": "",
    "deriv_app_id": "1089",
    "deriv_currency": "USD",
    "deriv_account_id": "",      # which Options account to trade (PAT API)
    "deriv_prefer_demo": True,   # pick the demo account when none is chosen
    # Trading
    # "live" or "paper". The UI no longer offers a switch — the app trades
    # live. Paper remains for dry-running a parser change; see the README.
    "mode": "live",
    "stake": 1.0,
    # Separate contracts opened per signal, each at the full stake. 3 x 50.00
    # is three independent 50.00 contracts, i.e. 150.00 at risk.
    "contracts_per_signal": 1,
    "default_duration": 15,
    "default_duration_unit": "m",
    "min_duration_minutes": 1,
    "max_duration_minutes": 240,
    # Guard rails
    "max_trades_per_day": 20,
    "max_daily_loss": 50.0,      # in account currency, 0 disables
    "max_concurrent_trades": 3,
    "max_signal_age_seconds": 90,
    "enabled": False,            # master switch for the listener
    # Headless hosts only: resume the listener on start-up if it was running
    # when the process died. Off by default — on a desktop you want to decide
    # deliberately; on a server you want it to survive a reboot.
    "auto_resume": False,
}

# Settings that can be supplied by the environment instead of the database.
# On a host with no persistent disk (Streamlit Cloud and friends) these are the
# only way to configure the app, since nothing written to SQLite survives.
_SECRET_ENV = {
    "deriv_token": "DERIV_TOKEN",
    "deriv_app_id": "DERIV_APP_ID",
    "deriv_account_id": "DERIV_ACCOUNT_ID",
    "tg_api_id": "TG_API_ID",
    "tg_api_hash": "TG_API_HASH",
    "tg_chat_id": "TG_CHAT_ID",
    "tg_chat_title": "TG_CHAT_TITLE",
    "mode": "MODE",
}

# A Telethon StringSession, for hosts where session/copier.session cannot
# persist. Generate one with tools/make_secrets.py.
SESSION_STRING_ENV = "TG_SESSION_STRING"


def session_string() -> str:
    return os.environ.get(SESSION_STRING_ENV, "").strip()


def overridden() -> Dict[str, str]:
    """Settings currently coming from the environment rather than the database.

    Worth surfacing: an override silently wins over anything typed into the
    UI, so a saved change would appear to do nothing.
    """
    out = {}
    for key, env_name in _SECRET_ENV.items():
        if os.environ.get(env_name):
            out[key] = env_name
    if os.environ.get(SESSION_STRING_ENV):
        out["tg_session"] = SESSION_STRING_ENV
    return out


@dataclass
class Config:
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_chat_id: str = ""
    tg_chat_title: str = ""
    deriv_token: str = ""
    deriv_app_id: str = "1089"
    deriv_currency: str = "USD"
    deriv_account_id: str = ""
    deriv_prefer_demo: bool = True
    mode: str = "live"
    stake: float = 1.0
    contracts_per_signal: int = 1
    default_duration: int = 15
    default_duration_unit: str = "m"
    min_duration_minutes: int = 1
    max_duration_minutes: int = 240
    max_trades_per_day: int = 20
    max_daily_loss: float = 50.0
    max_concurrent_trades: int = 3
    max_signal_age_seconds: int = 90
    enabled: bool = False
    auto_resume: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load() -> Config:
    values = {}
    for key, default in DEFAULTS.items():
        values[key] = db.get_setting(key, default)
    for key, env_name in _SECRET_ENV.items():
        env_value = os.environ.get(env_name)
        if env_value:
            values[key] = env_value
    return Config(**values)


def save(**changes: Any) -> None:
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise KeyError("unknown setting: {}".format(key))
        db.set_setting(key, value)


def ensure_session_dir() -> str:
    os.makedirs(SESSION_DIR, exist_ok=True)
    try:
        os.chmod(SESSION_DIR, 0o700)
    except OSError:
        pass
    return SESSION_PATH
