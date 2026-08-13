"""Telegram -> Deriv signal copier.

Run with:  ./.venv/bin/streamlit run app.py
"""

import datetime as dt
import os
import time

import pandas as pd
import streamlit as st

# Bridge Streamlit's secrets store into the environment before anything reads
# configuration. On a host with no persistent disk this is the only place the
# credentials can come from, since nothing written to SQLite survives a restart.
try:
    for _key in ("DERIV_TOKEN", "DERIV_APP_ID", "DERIV_ACCOUNT_ID",
                 "TG_API_ID", "TG_API_HASH", "TG_CHAT_ID", "TG_CHAT_TITLE",
                 "TG_SESSION_STRING"):
        if _key in st.secrets and _key not in os.environ:
            os.environ[_key] = str(st.secrets[_key])
except Exception:
    pass  # no secrets.toml configured — normal for a local run

from copier import config, db, deriv_rest, symbols
from copier.deriv import token_warning
from copier.engine import get_engine
from copier.parser import ParseError, direction_label, parse_signal

st.set_page_config(page_title="Telegram → Deriv Copier", page_icon="📈", layout="wide")


@st.cache_resource
def engine():
    return get_engine()


ENG = engine()
CFG = config.load()


def ts(value):
    if not value:
        return ""
    return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def run(coro, timeout=90.0):
    """Submit to the engine loop, surfacing failures as a Streamlit error."""
    try:
        return ENG.submit(coro, timeout=timeout)
    except Exception as exc:
        st.error("{}: {}".format(type(exc).__name__, exc))
        hint = deriv_rest.guidance_for(getattr(exc, "code", "") or "")
        if hint:
            st.info(hint)
        return None


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("📈 Signal Copier")

account = ENG.status.get("account") or {}
if account:
    kind = "DEMO" if account.get("is_virtual") else "REAL"
    st.sidebar.caption("Deriv: {} · {}".format(account.get("loginid", "?"), kind))
    if account.get("balance") is not None:
        st.sidebar.metric(
            "Balance", "{:.2f} {}".format(
                float(account["balance"]), account.get("currency", ""))
        )
else:
    st.sidebar.caption("Deriv: not connected")

if CFG.is_live:
    st.sidebar.error("LIVE MODE — real money")
else:
    st.sidebar.info("PAPER MODE — no orders sent")

st.sidebar.write("Listener: {}".format("🟢 running" if ENG.status["listening"] else "⚪️ stopped"))

page = st.sidebar.radio(
    "Page", ["Dashboard", "Connections", "Trading rules", "History"], label_visibility="collapsed"
)

if ENG.status.get("last_error"):
    st.sidebar.caption("Last issue: {}".format(ENG.status["last_error"][:120]))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def page_dashboard():
    st.title("Dashboard")

    ready = bool(CFG.tg_chat_id and CFG.deriv_token)
    if not ready:
        st.warning("Finish setup on the **Connections** page before starting the listener.")
    elif CFG.enabled and not ENG.status["listening"]:
        # The listener never resumes by itself — restarting into live trading
        # unattended is exactly the surprise you don't want.
        st.info("The listener was running when the app last shut down. "
                "Press **Start listener** to resume.")

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if ENG.status["listening"]:
            if st.button("⏹ Stop listener", type="primary", width="stretch"):
                run(ENG.stop_listening())
                st.rerun()
        else:
            if st.button("▶️ Start listener", type="primary",
                         disabled=not ready, width="stretch"):
                run(ENG.start_listening())
                st.rerun()
    with col2:
        if st.button("🔄 Reconnect Deriv", width="stretch"):
            run(ENG.connect_deriv())
            st.rerun()
    with col3:
        st.caption("Group: **{}**".format(CFG.tg_chat_title or "not selected"))
        st.caption("Stake **{:.2f} {}**{} · default expiry **{}{}** · mode **{}**".format(
            CFG.stake, CFG.deriv_currency,
            " × **{} contracts** = **{:.2f}**/signal".format(
                CFG.contracts_per_signal, CFG.stake * CFG.contracts_per_signal)
            if CFG.contracts_per_signal > 1 else "",
            CFG.default_duration, CFG.default_duration_unit, CFG.mode.upper()))

    auto = st.checkbox(
        "Auto-resume the listener when the app restarts", value=CFG.auto_resume,
        help="For an always-on server. If the process is restarted or the "
             "machine reboots while the listener was running, it starts itself "
             "again. Stopping it deliberately still keeps it stopped.",
    )
    if auto != CFG.auto_resume:
        config.save(auto_resume=auto)
        db.log("info", "ui", "Auto-resume {}".format("enabled" if auto else "disabled"))
        st.rerun()

    st.divider()

    @st.fragment(run_every="5s")
    def live_panels():
        stats = db.stats()
        settled = stats["wins"] + stats["losses"]
        win_rate = (stats["wins"] / settled * 100) if settled else 0.0

        a, b, c, d = st.columns(4)
        a.metric("Trades", stats["total"])
        b.metric("Win rate", "{:.0f}%".format(win_rate),
                 "{}W / {}L".format(stats["wins"], stats["losses"]))
        c.metric("Net P/L", "{:+.2f}".format(stats["pnl"]))
        d.metric("Errors", stats["errors"])

        today = db.trades_since(_day_start())
        today_pnl = sum(t["profit"] or 0.0 for t in today)
        st.caption("Today: {} trades, P/L {:+.2f} — limits: {} trades/day, {:.2f} max loss".format(
            len(today), today_pnl, CFG.max_trades_per_day, CFG.max_daily_loss))

        left, right = st.columns(2)

        with left:
            st.subheader("Open positions")
            rows = db.open_trades()
            if rows:
                st.dataframe(
                    pd.DataFrame([{
                        "Time": ts(r["created_at"]),
                        "Symbol": r["deriv_symbol"],
                        "Dir": "Rise" if r["contract_type"] == "CALL" else "Fall",
                        "Expiry": "{}{}".format(r["duration"], r["duration_unit"]),
                        "Stake": r["stake"],
                        "Payout": r["payout"],
                        "Mode": r["mode"],
                    } for r in rows]),
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("None open.")

            st.subheader("Recent signals")
            sigs = db.recent_signals(15)
            if sigs:
                st.dataframe(
                    pd.DataFrame([{
                        "Time": ts(r["received_at"]),
                        "Parsed": "{} {} {}{}".format(
                            r["pair"], direction_label(r["direction"]),
                            r["duration"], r["duration_unit"]
                        ) if r["parsed"] else "—",
                        "Status": r["status"],
                        "Note": (r["reason"] or "")[:60],
                    } for r in sigs]),
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("No messages seen yet.")

        with right:
            st.subheader("Recent trades")
            rows = db.recent_trades(15)
            if rows:
                st.dataframe(
                    pd.DataFrame([{
                        "Time": ts(r["created_at"]),
                        "Symbol": r["deriv_symbol"],
                        "Dir": "Rise" if r["contract_type"] == "CALL" else "Fall",
                        "Status": r["status"],
                        "P/L": r["profit"],
                        "Mode": r["mode"],
                    } for r in rows]),
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("No trades yet.")

            st.subheader("Activity log")
            events = db.recent_events(40)
            if events:
                st.dataframe(
                    pd.DataFrame([{
                        "Time": ts(r["ts"]),
                        "Level": r["level"],
                        "Source": r["source"],
                        "Message": r["message"],
                    } for r in events]),
                    hide_index=True, width="stretch", height=300,
                )
            else:
                st.caption("Nothing logged yet.")

    live_panels()


def _day_start():
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def page_connections():
    st.title("Connections")

    locked = config.overridden()
    if locked:
        st.info(
            "**{} setting(s) are coming from secrets, not the database:** {}.\n\n"
            "Environment values win, so editing them below will appear to save "
            "but change nothing. Edit `.streamlit/secrets.toml` (or your host's "
            "secrets store) instead — or delete that file to go back to the "
            "database.".format(len(locked), ", ".join("`{}`".format(env)
                                                      for env in sorted(locked.values())))
        )

    tab_tg, tab_deriv = st.tabs(["Telegram", "Deriv"])

    # ---------------- Telegram ----------------
    with tab_tg:
        st.subheader("1. API credentials")
        st.caption(
            "Get these once from https://my.telegram.org → API development tools. "
            "They identify the app, not your account."
        )
        with st.form("tg_creds"):
            c1, c2 = st.columns(2)
            api_id = c1.text_input("api_id", value=CFG.tg_api_id)
            api_hash = c2.text_input("api_hash", value=CFG.tg_api_hash, type="password")
            phone = st.text_input(
                "Phone number (international format)", value=CFG.tg_phone,
                placeholder="+27821234567",
            )
            if st.form_submit_button("Save credentials"):
                config.save(
                    tg_api_id=api_id.strip(), tg_api_hash=api_hash.strip(),
                    tg_phone=phone.strip(),
                )
                st.success("Saved.")
                st.rerun()

        st.subheader("2. Sign in")
        status = run(ENG.tg_status(), timeout=30) or {}
        if status.get("authorized"):
            st.success("Signed in as {}".format(status.get("user", "?")))
            if st.button("Log out of Telegram"):
                run(ENG.tg_logout())
                st.rerun()
        else:
            st.info("Not signed in. Telegram will text a login code to your phone.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Send login code", disabled=not (CFG.tg_api_id and CFG.tg_phone)):
                    result = run(ENG.tg_send_code(CFG.tg_phone), timeout=60)
                    if result == "code_sent":
                        st.session_state["tg_code_sent"] = True
                        st.success("Code sent — check Telegram.")
                    elif result == "already_authorized":
                        st.rerun()
            if st.session_state.get("tg_code_sent"):
                with st.form("tg_code"):
                    code = st.text_input("Login code")
                    if st.form_submit_button("Sign in"):
                        result = run(ENG.tg_sign_in(CFG.tg_phone, code.strip()), timeout=60)
                        if result == "password_needed":
                            st.session_state["tg_needs_2fa"] = True
                            st.warning("Two-step verification is on — enter your password below.")
                        elif result == "authorized":
                            st.session_state.pop("tg_code_sent", None)
                            st.rerun()
            if st.session_state.get("tg_needs_2fa"):
                with st.form("tg_2fa"):
                    password = st.text_input("Two-step password", type="password")
                    if st.form_submit_button("Confirm"):
                        if run(ENG.tg_sign_in_password(password), timeout=60) == "authorized":
                            st.session_state.pop("tg_needs_2fa", None)
                            st.session_state.pop("tg_code_sent", None)
                            st.rerun()

        st.subheader("3. Signal group")
        if not status.get("authorized"):
            st.caption("Sign in first to list your groups.")
        else:
            if st.button("Load my groups"):
                st.session_state["tg_chats"] = run(ENG.list_chats(), timeout=120)
            chats = st.session_state.get("tg_chats")
            if chats:
                labels = ["{} — {} [{}]".format(c["title"], c["kind"], c["id"]) for c in chats]
                current = 0
                for i, c in enumerate(chats):
                    if c["id"] == CFG.tg_chat_id:
                        current = i
                choice = st.selectbox("Group to copy from", labels, index=current)
                picked = chats[labels.index(choice)]
                if st.button("Use this group", type="primary"):
                    config.save(tg_chat_id=picked["id"], tg_chat_title=picked["title"])
                    st.success("Now copying from {}".format(picked["title"]))
                    st.rerun()
            elif CFG.tg_chat_title:
                st.caption("Currently set to **{}**".format(CFG.tg_chat_title))

        if CFG.tg_chat_id and status.get("authorized"):
            st.subheader("4. Parser check")
            st.caption("Reads recent messages and shows how each one would be understood. "
                       "Nothing is traded here.")
            if st.button("Test parser on recent messages"):
                msgs = run(ENG.fetch_recent_messages(CFG.tg_chat_id, 25), timeout=90)
                if msgs:
                    st.dataframe(
                        pd.DataFrame([{
                            "Message": m["text"][:80].replace("\n", " ⏎ "),
                            "Understood as": m["parsed"],
                        } for m in msgs]),
                        hide_index=True, width="stretch",
                    )
                else:
                    st.caption("No text messages found.")

    # ---------------- Deriv ----------------
    with tab_deriv:
        st.subheader("API token")
        st.caption(
            "Deriv issues two token formats and the app handles both. "
            "A **`pat_`** token from developers.deriv.com → API tokens uses the "
            "current REST + OTP flow. A legacy **`a1-`** token from "
            "app.deriv.com → Settings → API token uses the older WebSocket "
            "flow. Tick the **Trade** scope either way."
        )
        if CFG.deriv_token:
            st.caption("Stored token type: **{}**".format(
                "pat_ (current API)" if CFG.deriv_token.lower().startswith("pat_")
                else "a1- (legacy API)" if CFG.deriv_token.startswith("a1-")
                else "unrecognised"))
        with st.form("deriv_creds"):
            token = st.text_input("API token", value=CFG.deriv_token, type="password")
            app_id = st.text_input(
                "app_id", value=CFG.deriv_app_id,
                help="A registered Deriv app's id — not the token's name and "
                     "not the app's name. Current apps use a long alphanumeric "
                     "id such as k7Jd2Xq8mNpR4vTwYzB6c; legacy apps use a "
                     "number like 1089, which the current API rejects.",
            )
            # Saving and verifying are one action on purpose: with two buttons
            # it's far too easy to verify a stale token and misread the result.
            if st.form_submit_button("Save & connect", type="primary"):
                clean = token.strip()
                clean_app_id = app_id.strip() or "1089"
                config.save(deriv_token=clean, deriv_app_id=clean_app_id)
                warning = token_warning(clean)
                if not clean:
                    st.warning("No token entered.")
                elif warning:
                    st.error(warning)
                elif deriv_rest.app_id_warning(clean_app_id):
                    # "Nik" ends up here — the token's name, not an app id.
                    st.error(deriv_rest.app_id_warning(clean_app_id))
                else:
                    info = run(ENG.connect_deriv(), timeout=60)
                    if info:
                        st.success("Connected as {} ({}) — balance {:.2f} {}".format(
                            info.get("loginid"),
                            "DEMO" if info.get("is_virtual") else "REAL ACCOUNT",
                            float(info.get("balance") or 0),
                            info.get("currency") or CFG.deriv_currency))
                    else:
                        st.caption(
                            "`InvalidToken` means Deriv received the token and "
                            "did not recognise it. Copy it again using the copy "
                            "button on Deriv's API token page — the value shown "
                            "on screen is truncated. For more detail run "
                            "`./.venv/bin/python tools/check_deriv.py`."
                        )

        prefer_demo = st.checkbox(
            "Prefer the demo account when connecting", value=CFG.deriv_prefer_demo,
            help="With a pat_ token the app lists your Options accounts and picks "
                 "one. Leave this on until the integration is proven.",
        )
        if prefer_demo != CFG.deriv_prefer_demo:
            config.save(deriv_prefer_demo=prefer_demo)
            st.rerun()

        account = ENG.status.get("account") or {}
        if account.get("api"):
            st.caption("Connected via the **{}** API as **{}** ({}).".format(
                "current REST + OTP" if account["api"] == "pat" else "legacy WebSocket",
                account.get("loginid", "?"),
                "DEMO" if account.get("is_virtual") else "REAL MONEY"))

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Re-check connection", disabled=not CFG.deriv_token):
                info = run(ENG.connect_deriv(), timeout=60)
                if info:
                    st.success("Connected as {} ({}) — balance {:.2f} {}".format(
                        info.get("loginid"),
                        "DEMO" if info.get("is_virtual") else "REAL ACCOUNT",
                        float(info.get("balance") or 0), CFG.deriv_currency))
        with c2:
            if st.button("Refresh tradable symbols", disabled=not CFG.deriv_token):
                count = run(ENG.refresh_symbols(), timeout=90)
                if count:
                    st.success("Cached {} symbols.".format(count))

        cached = symbols.load_active_symbols()
        if cached:
            st.caption("{} symbols cached.".format(len(cached)))
            with st.expander("Browse cached symbols"):
                query = st.text_input("Filter", "frx")
                shown = [s for s in cached if query.lower() in s["symbol"].lower()
                         or query.lower() in s["display_name"].lower()][:200]
                st.dataframe(
                    pd.DataFrame([{
                        "Symbol": s["symbol"],
                        "Name": s["display_name"],
                        "Market": s["market"],
                        "Open": "yes" if s["exchange_is_open"] else "no",
                    } for s in shown]),
                    hide_index=True, width="stretch",
                )
        else:
            st.warning("No symbol list cached yet — click **Refresh tradable symbols**. "
                       "Until then, pairs are mapped by naming convention only.")


# ---------------------------------------------------------------------------
# Trading rules
# ---------------------------------------------------------------------------

def _save_mode(mode):
    """Save the trading mode and prove it stuck.

    A silently-failing write looks exactly like a toggle that won't move: the
    next rerun reloads the old value and the switch springs back. That happens
    on hosts with a read-only or ephemeral filesystem, so say so plainly
    instead of leaving you clicking at it.
    """
    try:
        config.save(mode=mode)
    except Exception as exc:
        st.error("Could not save the setting: {}: {}".format(type(exc).__name__, exc))
        return False

    if config.load().mode != mode:
        st.error(
            "The setting did not persist — `{}` is not writable on this host, "
            "so the switch springs back. This app keeps all of its state "
            "(settings, API token, Telegram session, trade history) in a local "
            "SQLite file and needs a writable disk. Run it on your own machine "
            "or a VPS rather than an ephemeral hosting platform.".format(db.DB_PATH)
        )
        return False
    return True


def page_rules():
    st.title("Trading rules")

    acct = ENG.status.get("account") or {}
    if CFG.is_live:
        st.error(
            "**Live trading.** Every signal places a real order on account "
            "**{}**{}.".format(
                acct.get("loginid", "not connected"),
                "" if acct.get("is_virtual") else " — a REAL-MONEY account",
            )
        )
    else:
        st.info(
            "**Paper mode.** Signals are parsed and priced but no order is "
            "placed. Re-enable live trading with:  "
            "`./.venv/bin/python -m copier.mode live`"
        )

    st.divider()

    with st.form("rules"):
        st.subheader("Risk per trade")
        c1, c2 = st.columns(2)
        stake = c1.number_input(
            "Stake ({})".format(CFG.deriv_currency), min_value=0.35,
            value=float(CFG.stake), step=0.5, format="%.2f",
            help="Amount per contract. Deriv's minimum is usually 0.35 USD.",
        )
        contracts = c2.number_input(
            "Contracts per signal", min_value=1, max_value=5,
            value=int(CFG.contracts_per_signal), step=1,
            help="Open this many separate contracts on the same asset for each "
                 "signal, each at the full stake. They are priced, filled and "
                 "settled independently, and each one counts towards your daily "
                 "and open-trade limits.",
        )
        st.caption(
            "Currency comes from the connected Deriv account: **{}** — "
            "**{:.2f} {} at risk per signal** ({} × {:.2f})".format(
                CFG.deriv_currency, stake * contracts, CFG.deriv_currency,
                contracts, stake)
        )
        if contracts > 1 and CFG.max_concurrent_trades:
            signals_at_once = CFG.max_concurrent_trades // contracts
            if signals_at_once < 1:
                st.warning(
                    "**Max open trades ({}) is below {} contracts**, so every "
                    "signal will be cut short. Raise it to at least {}.".format(
                        CFG.max_concurrent_trades, contracts, contracts))
            else:
                st.caption("Max open trades ({}) allows about {} signal(s) "
                           "running at once.".format(
                               CFG.max_concurrent_trades, signals_at_once))

        st.subheader("Expiry")
        st.caption("Each signal's own duration is used. These apply when a message "
                   "states no expiry, and bound anything unusual.")
        c1, c2, c3 = st.columns(3)
        default_duration = c1.number_input(
            "Fallback duration", min_value=1, value=int(CFG.default_duration), step=1)
        default_unit = c2.selectbox(
            "Unit", ["m", "h", "s", "t", "d"],
            index=["m", "h", "s", "t", "d"].index(CFG.default_duration_unit))
        c3.write("")
        c1, c2 = st.columns(2)
        min_minutes = c1.number_input(
            "Minimum expiry (minutes)", min_value=0, value=int(CFG.min_duration_minutes))
        max_minutes = c2.number_input(
            "Maximum expiry (minutes)", min_value=1, value=int(CFG.max_duration_minutes))

        st.subheader("Guard rails")
        c1, c2 = st.columns(2)
        max_trades = c1.number_input(
            "Max trades per day", min_value=0, value=int(CFG.max_trades_per_day),
            help="0 = unlimited.")
        max_loss = c2.number_input(
            "Max daily loss ({})".format(CFG.deriv_currency), min_value=0.0,
            value=float(CFG.max_daily_loss), step=5.0, format="%.2f",
            help="0 = no limit. Once today's P/L drops below this, new signals are skipped.")
        c1, c2 = st.columns(2)
        max_concurrent = c1.number_input(
            "Max open trades at once", min_value=0, value=int(CFG.max_concurrent_trades))
        max_age = c2.number_input(
            "Ignore signals older than (seconds)", min_value=0,
            value=int(CFG.max_signal_age_seconds),
            help="Protects against old messages being replayed after a reconnect.")

        if st.form_submit_button("Save rules", type="primary"):
            config.save(
                stake=float(stake),
                contracts_per_signal=int(contracts),
                default_duration=int(default_duration),
                default_duration_unit=default_unit,
                min_duration_minutes=int(min_minutes),
                max_duration_minutes=int(max_minutes),
                max_trades_per_day=int(max_trades),
                max_daily_loss=float(max_loss),
                max_concurrent_trades=int(max_concurrent),
                max_signal_age_seconds=int(max_age),
            )
            st.success("Saved.")
            st.rerun()

    st.divider()
    st.subheader("Try a signal")
    st.caption("Paste a message to see how it parses, then optionally place that "
               "one trade in the current mode.")
    sample = st.text_input("Message text", "AUDCHF 15 minutes Down")
    parsed = None
    if sample.strip():
        try:
            parsed = parse_signal(sample, CFG.default_duration, CFG.default_duration_unit)
            resolved = symbols.resolve(parsed.pair)
            st.success("{} → **{}** {} for {}{}".format(
                parsed.pair, resolved or "UNKNOWN SYMBOL",
                "Rise" if parsed.direction == "CALL" else "Fall",
                parsed.duration, parsed.duration_unit))
        except ParseError as exc:
            st.error("Not a signal: {}".format(exc))

    if parsed and st.button("Place this one trade ({})".format(CFG.mode.upper())):
        result = run(ENG.test_trade(
            parsed.pair, parsed.direction, parsed.duration, parsed.duration_unit
        ), timeout=90)
        if result:
            st.success("Submitted on {} — see the Dashboard.".format(result))


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def page_history():
    st.title("History")

    tab_trades, tab_signals, tab_log = st.tabs(["Trades", "Signals", "Log"])

    with tab_trades:
        rows = db.recent_trades(500)
        if not rows:
            st.caption("No trades recorded.")
        else:
            frame = pd.DataFrame([{
                "Time": ts(r["created_at"]),
                "Mode": r["mode"],
                "Symbol": r["deriv_symbol"],
                "Direction": "Rise" if r["contract_type"] == "CALL" else "Fall",
                "Expiry": "{}{}".format(r["duration"], r["duration_unit"]),
                "Stake": r["stake"],
                "Payout": r["payout"],
                "Status": r["status"],
                "P/L": r["profit"],
                "Entry": r["entry_spot"],
                "Exit": r["exit_spot"],
                "Contract": r["contract_id"],
                "Error": r["error"],
            } for r in rows])
            st.dataframe(frame, hide_index=True, width="stretch")
            st.download_button(
                "Download CSV", frame.to_csv(index=False).encode(),
                "trades.csv", "text/csv",
            )

            settled = [r for r in rows if r["profit"] is not None]
            if settled:
                curve = pd.DataFrame({
                    "Trade": range(1, len(settled) + 1),
                    "Cumulative P/L": pd.Series(
                        [r["profit"] for r in reversed(settled)]).cumsum(),
                })
                st.line_chart(curve, x="Trade", y="Cumulative P/L")

    with tab_signals:
        rows = db.recent_signals(500)
        if not rows:
            st.caption("No signals recorded.")
        else:
            st.dataframe(pd.DataFrame([{
                "Time": ts(r["received_at"]),
                "Message": (r["raw_text"] or "")[:100].replace("\n", " ⏎ "),
                "Pair": r["pair"],
                "Direction": direction_label(r["direction"]),
                "Expiry": "{}{}".format(r["duration"], r["duration_unit"])
                          if r["parsed"] else "",
                "Status": r["status"],
                "Reason": r["reason"],
            } for r in rows]), hide_index=True, width="stretch")

    with tab_log:
        rows = db.recent_events(500)
        st.dataframe(pd.DataFrame([{
            "Time": ts(r["ts"]),
            "Level": r["level"],
            "Source": r["source"],
            "Message": r["message"],
        } for r in rows]), hide_index=True, width="stretch")


PAGES = {
    "Dashboard": page_dashboard,
    "Connections": page_connections,
    "Trading rules": page_rules,
    "History": page_history,
}
PAGES[page]()
