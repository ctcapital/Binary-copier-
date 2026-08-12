"""Engine tests against a stubbed Deriv client — no network, no real money.

Run: ./.venv/bin/python tests/test_engine.py
"""

import asyncio
import datetime as dt
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the DB at a throwaway directory before anything imports it for real.
_TMP = tempfile.mkdtemp(prefix="copier-test-")
from copier import db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = os.path.join(_TMP, "test.db")

from copier import config, engine as engine_mod, symbols  # noqa: E402
from copier.deriv import DerivError  # noqa: E402

FAILURES = []


def check(condition, label):
    if condition:
        print("  ok   {}".format(label))
    else:
        print("  FAIL {}".format(label))
        FAILURES.append(label)


class FakeDeriv:
    """Stands in for DerivClient: prices contracts and serves spot ticks."""

    def __init__(self):
        self.spot_value = 100.0
        self.buys = []
        self.fail_with = None

    async def proposal(self, symbol, contract_type, amount, currency, duration, duration_unit):
        if self.fail_with:
            raise self.fail_with
        return {"id": "prop-1", "ask_price": amount, "payout": amount * 1.9,
                "longcode": "Win payout if {} rises".format(symbol)}

    async def place_trade(self, symbol, contract_type, stake, currency, duration, duration_unit):
        if self.fail_with:
            raise self.fail_with
        self.buys.append((symbol, contract_type, stake, duration, duration_unit))
        return {"contract_id": "c{}".format(len(self.buys)), "buy_price": stake,
                "payout": stake * 1.9, "longcode": "lc", "start_time": int(time.time())}

    async def send(self, payload, timeout=30):
        if "ticks_history" in payload:
            return {"history": {"prices": [self.spot_value]}}
        return {}

    async def contract_status(self, contract_id):
        return {"is_sold": 1, "profit": 0.9, "entry_spot": 100.0, "exit_spot": 101.0}


class FakeMessage:
    def __init__(self, message_id, text, age_seconds=0):
        self.id = message_id
        self.message = text
        self.date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)


class FakeEvent:
    def __init__(self, message):
        self.message = message


def reset_settings(**overrides):
    base = {
        "mode": "paper", "stake": 10.0, "deriv_currency": "USD",
        "tg_chat_id": "-100123", "deriv_token": "fake-token",
        "max_trades_per_day": 20, "max_daily_loss": 100.0,
        "max_concurrent_trades": 3, "max_signal_age_seconds": 90,
        "min_duration_minutes": 1, "max_duration_minutes": 240,
        "default_duration": 15, "default_duration_unit": "m",
        "contracts_per_signal": 1,
    }
    base.update(overrides)
    config.save(**base)


def clear_tables():
    conn = db.connect()
    try:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM events")
        conn.commit()
    finally:
        conn.close()


def main():
    db.init()
    symbols.save_active_symbols([
        {"symbol": "frxAUDCHF", "display_name": "AUD/CHF", "market": "forex", "exchange_is_open": 1},
        {"symbol": "frxEURGBP", "display_name": "EUR/GBP", "market": "forex", "exchange_is_open": 1},
        {"symbol": "frxUSDJPY", "display_name": "USD/JPY", "market": "forex", "exchange_is_open": 1},
        {"symbol": "R_100", "display_name": "Volatility 100", "market": "synthetic_index", "exchange_is_open": 1},
    ])

    eng = engine_mod.Engine()
    fake = FakeDeriv()

    async def fake_client():
        return fake

    eng._deriv_client = fake_client

    def handle(message):
        return eng.submit(eng._handle_message(FakeEvent(message)), timeout=30)

    # ---------------------------------------------------------------
    print("\n[1] paper trade from a normal signal")
    reset_settings()
    clear_tables()
    handle(FakeMessage(1, "AUDCHF 15 minutes Down"))
    trades = db.recent_trades()
    check(len(trades) == 1, "one trade recorded")
    if trades:
        t = trades[0]
        check(t["deriv_symbol"] == "frxAUDCHF", "symbol mapped to frxAUDCHF")
        check(t["contract_type"] == "PUT", "Down -> PUT")
        check(t["duration"] == 15 and t["duration_unit"] == "m", "expiry 15m from signal")
        check(t["mode"] == "paper" and t["status"] == "open", "opened in paper mode")
        check(t["entry_spot"] == 100.0, "entry spot captured")
        check(len(fake.buys) == 0, "paper mode sent no buy order")

    # ---------------------------------------------------------------
    print("\n[2] duplicate message is ignored")
    handle(FakeMessage(1, "AUDCHF 15 minutes Down"))
    check(len(db.recent_trades()) == 1, "replayed message did not trade again")

    # ---------------------------------------------------------------
    print("\n[3] chatter is ignored, not traded")
    clear_tables()
    handle(FakeMessage(2, "good luck everyone today"))
    check(len(db.recent_trades()) == 0, "non-signal produced no trade")
    sigs = db.recent_signals()
    check(len(sigs) == 1 and sigs[0]["status"] == "ignored", "logged as ignored")

    # ---------------------------------------------------------------
    print("\n[4] stale signals are skipped")
    clear_tables()
    handle(FakeMessage(3, "EURGBP 15 minutes Up", age_seconds=600))
    check(len(db.recent_trades()) == 0, "10-minute-old signal not traded")
    check(db.recent_signals()[0]["status"] == "skipped", "marked skipped")

    # ---------------------------------------------------------------
    print("\n[5] daily trade cap")
    clear_tables()
    reset_settings(max_trades_per_day=2, max_concurrent_trades=99)
    for i in range(4):
        handle(FakeMessage(100 + i, "EURGBP 15 minutes Up"))
    check(len(db.recent_trades()) == 2, "stopped at the 2-trade daily cap")

    # ---------------------------------------------------------------
    print("\n[6] concurrent-trade cap")
    clear_tables()
    reset_settings(max_concurrent_trades=1, max_trades_per_day=0)
    handle(FakeMessage(200, "EURGBP 15 minutes Up"))
    handle(FakeMessage(201, "AUDCHF 15 minutes Down"))
    check(len(db.recent_trades()) == 1, "second signal blocked while one is open")

    # ---------------------------------------------------------------
    print("\n[7] daily loss limit")
    clear_tables()
    reset_settings(max_daily_loss=20.0, max_concurrent_trades=99, max_trades_per_day=0)
    tid = db.insert_trade(None, "paper", "frxEURGBP", "CALL", 10.0, "USD", 15, "m", "lost")
    db.update_trade(tid, profit=-25.0, settled_at=time.time())
    handle(FakeMessage(300, "EURGBP 15 minutes Up"))
    check(len(db.recent_trades()) == 1, "no new trade after the loss limit was hit")

    # ---------------------------------------------------------------
    print("\n[8] expiry outside the allowed band")
    clear_tables()
    reset_settings(min_duration_minutes=5, max_duration_minutes=60,
                   max_concurrent_trades=99, max_trades_per_day=0)
    handle(FakeMessage(400, "EURGBP 1 minute Up"))
    check(len(db.recent_trades()) == 0, "1m signal rejected below the 5m minimum")
    handle(FakeMessage(401, "EURGBP 5 hours Up"))
    check(len(db.recent_trades()) == 0, "5h signal rejected above the 60m maximum")

    # ---------------------------------------------------------------
    print("\n[9] paper settlement pays out correctly")
    clear_tables()
    reset_settings(max_concurrent_trades=99, max_trades_per_day=0)
    fake.spot_value = 100.0
    handle(FakeMessage(500, "R_100 1 minute Up"))   # CALL, entry 100
    trade = db.recent_trades()[0]
    # Backdate so the contract has expired, then settle with a higher price.
    conn = db.connect()
    conn.execute("UPDATE trades SET created_at = ? WHERE id = ?",
                 (time.time() - 120, trade["id"]))
    conn.commit()
    conn.close()
    fake.spot_value = 101.0
    eng.submit(eng._settle_once(), timeout=30)
    settled = db.recent_trades()[0]
    check(settled["status"] == "won", "rising price settles a CALL as a win")
    check(abs((settled["profit"] or 0) - 9.0) < 0.01,
          "profit = payout - stake ({})".format(settled["profit"]))

    print("\n[10] paper settlement on a losing side")
    clear_tables()
    fake.spot_value = 100.0
    handle(FakeMessage(501, "R_100 1 minute Down"))  # PUT, entry 100
    trade = db.recent_trades()[0]
    conn = db.connect()
    conn.execute("UPDATE trades SET created_at = ? WHERE id = ?",
                 (time.time() - 120, trade["id"]))
    conn.commit()
    conn.close()
    fake.spot_value = 101.0   # price rose, so a PUT loses
    eng.submit(eng._settle_once(), timeout=30)
    settled = db.recent_trades()[0]
    check(settled["status"] == "lost", "rising price settles a PUT as a loss")
    check(abs((settled["profit"] or 0) + 10.0) < 0.01, "loss equals the stake")

    # ---------------------------------------------------------------
    print("\n[11] live mode places a real buy")
    clear_tables()
    reset_settings(mode="live", max_concurrent_trades=99, max_trades_per_day=0)
    fake.buys.clear()
    handle(FakeMessage(600, "AUDCHF 15 minutes Down"))
    check(len(fake.buys) == 1, "buy order sent in live mode")
    if fake.buys:
        check(fake.buys[0] == ("frxAUDCHF", "PUT", 10.0, 15, "m"),
              "buy carried the right symbol/direction/stake/expiry")
    t = db.recent_trades()[0]
    check(t["status"] == "open" and t["contract_id"] == "c1", "contract id stored")

    # ---------------------------------------------------------------
    print("\n[12] a Deriv rejection is recorded, not swallowed")
    clear_tables()
    fake.fail_with = DerivError("MarketIsClosed", "This market is presently closed.")
    handle(FakeMessage(700, "AUDCHF 15 minutes Down"))
    t = db.recent_trades()[0]
    check(t["status"] == "error", "trade marked as error")
    check("closed" in (t["error"] or ""), "error message preserved")
    check(db.recent_signals()[0]["status"] == "error", "signal marked as error")
    fake.fail_with = None

    # ---------------------------------------------------------------
    print("\n[13] the group's result posts are labelled, never traded")
    clear_tables()
    reset_settings(max_concurrent_trades=99, max_trades_per_day=0)
    fake.buys.clear()
    results = [
        "EURGBP Positive signal! ✅",
        "AUDJPY Negative signal! ➖",
        "EURCHF Negative signal! ➖",
        "EURJPY Positive signal! ✅",
    ]
    for i, text in enumerate(results):
        handle(FakeMessage(900 + i, text))
    check(len(db.recent_trades()) == 0, "no trade placed on any result post")
    check(len(fake.buys) == 0, "no buy order sent")
    rows = db.recent_signals()
    check(len(rows) == len(results), "all four recorded")
    check(all(r["status"] == "result" for r in rows),
          "labelled 'result', not 'ignored'")

    print("\n[14] a real signal alongside results still trades")
    clear_tables()
    handle(FakeMessage(950, "EURGBP Positive signal! ✅"))
    handle(FakeMessage(951, "USDJPY 15 minutes Down"))
    trades = db.recent_trades()
    check(len(trades) == 1, "exactly one trade from the real signal")
    if trades:
        check(trades[0]["deriv_symbol"] == "frxUSDJPY"
              and trades[0]["contract_type"] == "PUT",
              "traded the signal, not the result post")

    # ---------------------------------------------------------------
    print("\n[15] multiple contracts per signal")
    clear_tables()
    reset_settings(mode="live", stake=50.0, contracts_per_signal=3,
                   max_concurrent_trades=99, max_trades_per_day=0)
    fake.buys.clear()
    handle(FakeMessage(1000, "AUDCHF 15 minutes Down"))
    trades = db.recent_trades()
    check(len(trades) == 3, "3 separate contracts opened ({})".format(len(trades)))
    check(len(fake.buys) == 3, "3 buy orders sent to Deriv")
    check(all(t["stake"] == 50.0 for t in trades), "each at the full 50.00 stake")
    check(sum(t["stake"] for t in trades) == 150.0, "150.00 total at risk")
    check(len({t["contract_id"] for t in trades}) == 3, "3 distinct contract ids")
    check(db.recent_signals()[0]["status"] == "executed", "signal marked executed")

    print("\n[16] contracts still respect the open-trade cap")
    clear_tables()
    reset_settings(mode="live", stake=50.0, contracts_per_signal=5,
                   max_concurrent_trades=2, max_trades_per_day=0)
    fake.buys.clear()
    handle(FakeMessage(1001, "AUDCHF 15 minutes Down"))
    check(len(db.recent_trades()) == 2,
          "asked for 5, capped at 2 open ({})".format(len(db.recent_trades())))
    sig = db.recent_signals()[0]
    check(sig["status"] == "executed" and "2 of 5" in (sig["reason"] or ""),
          "partial fill recorded as '2 of 5' (got {!r})".format(sig["reason"]))

    print("\n[17] contracts count against the daily cap")
    clear_tables()
    reset_settings(mode="live", stake=50.0, contracts_per_signal=3,
                   max_concurrent_trades=99, max_trades_per_day=4)
    fake.buys.clear()
    handle(FakeMessage(1002, "AUDCHF 15 minutes Down"))   # 3 contracts
    handle(FakeMessage(1003, "EURGBP 15 minutes Up"))     # only 1 left
    check(len(db.recent_trades()) == 4,
          "stopped at the 4-trade daily cap ({})".format(len(db.recent_trades())))

    print("\n[18] paper mode opens multiple contracts too")
    clear_tables()
    reset_settings(mode="paper", stake=10.0, contracts_per_signal=2,
                   max_concurrent_trades=99, max_trades_per_day=0)
    fake.buys.clear()
    handle(FakeMessage(1004, "AUDCHF 15 minutes Down"))
    check(len(db.recent_trades()) == 2, "2 paper contracts")
    check(len(fake.buys) == 0, "no live buys in paper mode")

    # ---------------------------------------------------------------
    print("\n[19] unknown pair never reaches Deriv")
    clear_tables()
    reset_settings(max_concurrent_trades=99, max_trades_per_day=0)
    fake.buys.clear()
    handle(FakeMessage(800, "ZZZQQQ 15 minutes Up"))
    check(len(db.recent_trades()) == 0, "unmapped pair produced no trade")

    print("\n" + "=" * 60)
    if FAILURES:
        print("FAILED {} check(s):".format(len(FAILURES)))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("All engine checks passed.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
