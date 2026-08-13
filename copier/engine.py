"""The runtime: one background thread owning an asyncio loop, the Telegram
client and the Deriv client.

Streamlit re-runs its script on every interaction, so nothing long-lived can
live in the script itself. The UI holds a single cached Engine and talks to it
by submitting coroutines onto its loop; all durable state lands in SQLite.
"""

import asyncio
import datetime as dt
import threading
import time
from typing import Any, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.errors import AuthKeyDuplicatedError, SessionPasswordNeededError
from telethon.sessions import StringSession

from . import config, db, symbols
from .deriv import DerivClient, DerivError
from .parser import ParseError, direction_label, looks_like_result, parse_signal

SETTLE_POLL_SECONDS = 5.0
UNIT_SECONDS = {"t": 2, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _day_start_ts() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def duration_to_seconds(duration: int, unit: str) -> int:
    return int(duration) * UNIT_SECONDS.get(unit, 60)


def _of(index: int, total: int) -> str:
    """' (2/3)' when a signal opens several contracts, blank when it's one."""
    return "" if total <= 1 else " ({}/{})".format(index, total)


class Engine:
    # Bumped whenever the interface the UI depends on changes. Streamlit can
    # reload app.py without restarting the process, leaving a cached Engine
    # from the previous build in place; the UI compares this and says so
    # rather than failing with an unreadable TypeError.
    API_VERSION = 2

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_forever, name="copier-engine", daemon=True
        )
        self._thread.start()

        self.tg: Optional[TelegramClient] = None
        self.deriv: Optional[DerivClient] = None

        self._handler = None
        self._settle_task = None
        self._phone_code_hash: Optional[str] = None

        self.status: Dict[str, Any] = {
            "tg_connected": False,
            "tg_authorized": False,
            "listening": False,
            "deriv_connected": False,
            "account": {},
            "last_signal_at": None,
            "last_error": "",
            "started_at": time.time(),
        }

    # ------------------------------------------------------------------
    # loop plumbing
    # ------------------------------------------------------------------

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, timeout: float = 90.0):
        """Run a coroutine on the engine loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def log(self, level: str, message: str, source: str = "engine") -> None:
        db.log(level, source, message)
        if level in ("error", "warning"):
            self.status["last_error"] = "{}: {}".format(level, message)

    def _deriv_logger(self, level: str, message: str) -> None:
        self.log(level, message, source="deriv")

    # ------------------------------------------------------------------
    # Deriv
    # ------------------------------------------------------------------

    async def _deriv_client(self) -> DerivClient:
        """Get a live, authorized Deriv client. Built on the engine loop so its
        asyncio primitives bind to the right loop."""
        cfg = config.load()
        if not cfg.deriv_token:
            raise RuntimeError("No Deriv API token configured")

        stale = self.deriv is not None and (
            self.deriv.token != cfg.deriv_token
            or self.deriv.app_id != str(cfg.deriv_app_id)
            or self.deriv.account_id != cfg.deriv_account_id
        )
        if stale:
            await self.deriv.close()
            self.deriv = None

        if self.deriv is None:
            self.deriv = DerivClient(
                token=cfg.deriv_token,
                app_id=cfg.deriv_app_id,
                logger=self._deriv_logger,
                account_id=cfg.deriv_account_id,
                prefer_demo=cfg.deriv_prefer_demo,
            )

        account = await self.deriv.connect()
        self.status["deriv_connected"] = True
        self.status["account"] = dict(account)
        return self.deriv

    async def connect_deriv(self) -> Dict[str, Any]:
        client = await self._deriv_client()

        # The authorize reply already carries currency and balance. Refreshing
        # via balance() is a bonus, and a token without the read scope must not
        # fail the whole connection over it.
        account = self.status["account"]
        try:
            balance = await client.balance()
            if balance.get("balance") is not None:
                account["balance"] = balance["balance"]
            if balance.get("currency"):
                account["currency"] = balance["currency"]
        except (DerivError, ConnectionError, TimeoutError) as exc:
            self.log("warning", "Connected, but balance is unavailable: {}. "
                                "The token may lack the read scope.".format(exc))

        if account.get("currency"):
            config.save(deriv_currency=account["currency"])

        self.log("info", "Deriv connected as {} ({})".format(
            account.get("loginid"),
            "demo" if account.get("is_virtual") else "REAL",
        ))
        return account

    async def refresh_symbols(self) -> int:
        client = await self._deriv_client()
        active = await client.active_symbols()
        symbols.save_active_symbols(active)
        self.log("info", "Cached {} Deriv symbols".format(len(active)))
        return len(active)

    async def spot(self, symbol: str) -> Optional[float]:
        """Latest tick for a symbol — used to price paper trades."""
        client = await self._deriv_client()
        data = await client.send({
            "ticks_history": symbol,
            "end": "latest",
            "count": 1,
            "style": "ticks",
        })
        prices = (data.get("history") or {}).get("prices") or []
        return float(prices[-1]) if prices else None

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    async def _tg_client(self) -> TelegramClient:
        cfg = config.load()
        if not cfg.tg_api_id or not cfg.tg_api_hash:
            raise RuntimeError("Telegram api_id / api_hash not configured")
        if self.tg is None:
            # A StringSession keeps the login in an env var rather than on
            # disk, which is the only way to stay signed in on a host with no
            # persistent filesystem.
            stored = config.session_string()
            session = StringSession(stored) if stored else config.ensure_session_dir()
            self.tg = TelegramClient(
                session, int(cfg.tg_api_id), cfg.tg_api_hash
            )
        if not self.tg.is_connected():
            await self.tg.connect()
        self.status["tg_connected"] = True
        return self.tg

    async def tg_status(self) -> Dict[str, Any]:
        try:
            client = await self._tg_client()
        except AuthKeyDuplicatedError:
            # Telegram kills a key used from two IPs at once — and kills both
            # copies, so the other instance is broken too.
            self.tg = None
            self.status["tg_connected"] = False
            self.status["tg_authorized"] = False
            return {
                "connected": False, "authorized": False,
                "duplicated": True,
                "error": "This Telegram login was used from two machines at "
                         "once, so Telegram cancelled it. Both copies are now "
                         "dead. Sign in again here, and give every other "
                         "instance its own separate login.",
            }
        except Exception as exc:
            self.status["tg_connected"] = False
            self.status["tg_authorized"] = False
            return {"connected": False, "authorized": False, "error": str(exc)}
        authorized = await client.is_user_authorized()
        self.status["tg_authorized"] = authorized
        out = {"connected": True, "authorized": authorized, "error": ""}
        if authorized:
            me = await client.get_me()
            out["user"] = "{} (@{})".format(
                (me.first_name or "").strip(), me.username or "no username"
            )
        return out

    async def tg_send_code(self, phone: str) -> str:
        client = await self._tg_client()
        if await client.is_user_authorized():
            self.status["tg_authorized"] = True
            return "already_authorized"
        sent = await client.send_code_request(phone)
        self._phone_code_hash = sent.phone_code_hash
        self.log("info", "Telegram login code requested for {}".format(phone))
        return "code_sent"

    async def tg_sign_in(self, phone: str, code: str) -> str:
        client = await self._tg_client()
        try:
            await client.sign_in(
                phone=phone, code=code, phone_code_hash=self._phone_code_hash
            )
        except SessionPasswordNeededError:
            return "password_needed"
        self.status["tg_authorized"] = True
        self.log("info", "Telegram sign-in complete")
        return "authorized"

    async def tg_sign_in_password(self, password: str) -> str:
        client = await self._tg_client()
        await client.sign_in(password=password)
        self.status["tg_authorized"] = True
        self.log("info", "Telegram 2FA sign-in complete")
        return "authorized"

    async def export_session_string(self) -> str:
        """Serialise this instance's own login so it survives a restart.

        On a host with no persistent disk the signed-in session is lost every
        time the process restarts. Saving this string back into the host's
        secrets lets the same instance resume its own login.

        This is the same key, not a second one — safe to reuse *here*, but
        running it anywhere else at the same time makes Telegram cancel both.
        """
        client = await self._tg_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Not signed in — complete the login first")

        portable = StringSession()
        portable.set_dc(client.session.dc_id, client.session.server_address,
                        client.session.port)
        portable.auth_key = client.session.auth_key
        return portable.save()

    async def tg_logout(self) -> None:
        if self.tg is not None:
            await self.stop_listening()
            await self.tg.log_out()
            self.tg = None
        self.status["tg_authorized"] = False
        self.status["tg_connected"] = False

    async def list_chats(self, limit: int = 300) -> List[Dict[str, Any]]:
        client = await self._tg_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram is not signed in yet")
        out = []
        async for dialog in client.iter_dialogs(limit=limit):
            if dialog.is_group or dialog.is_channel:
                out.append({
                    "id": str(dialog.id),
                    "title": dialog.name or "(untitled)",
                    "kind": "channel" if dialog.is_channel else "group",
                })
        return out

    async def fetch_recent_messages(self, chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Pull recent history so you can dry-run the parser on real messages."""
        client = await self._tg_client()
        out = []
        async for message in client.iter_messages(int(chat_id), limit=limit):
            text = message.message or ""
            if not text.strip():
                continue
            entry = {"id": message.id, "date": message.date, "text": text}
            try:
                cfg = config.load()
                signal = parse_signal(
                    text, cfg.default_duration, cfg.default_duration_unit
                )
                entry["parsed"] = "{} {} {}{}".format(
                    signal.pair, direction_label(signal.direction),
                    signal.duration, signal.duration_unit
                )
            except ParseError as exc:
                entry["parsed"] = ("— trade result (ignored)"
                                   if looks_like_result(text)
                                   else "— ({})".format(exc))
            out.append(entry)
        return out

    # ------------------------------------------------------------------
    # listener
    # ------------------------------------------------------------------

    async def start_listening(self) -> None:
        cfg = config.load()
        if not cfg.tg_chat_id:
            raise RuntimeError("No Telegram group selected")

        client = await self._tg_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram is not signed in yet")

        await self.stop_listening()

        chat_id = int(cfg.tg_chat_id)

        async def _on_message(event) -> None:
            try:
                await self._handle_message(event)
            except Exception as exc:  # never let a bad message kill the handler
                self.log("error", "handler crashed: {}".format(exc))

        self._handler = _on_message
        client.add_event_handler(_on_message, events.NewMessage(chats=chat_id))

        if self._settle_task is None or self._settle_task.done():
            self._settle_task = asyncio.ensure_future(self._settle_loop())

        self.status["listening"] = True
        config.save(enabled=True)
        self.log("info", "Listening to {} in {} mode".format(
            cfg.tg_chat_title or chat_id, cfg.mode.upper()))

    async def stop_listening(self) -> None:
        if self.tg is not None and self._handler is not None:
            try:
                self.tg.remove_event_handler(self._handler)
            except Exception:
                pass
        self._handler = None
        self.status["listening"] = False
        config.save(enabled=False)

    def maybe_auto_resume(self) -> None:
        """Restart the listener after a reboot, but only on an opt-in host.

        `enabled` is cleared whenever the listener is stopped deliberately, so
        this only ever resumes something that was cut off mid-run.
        """
        cfg = config.load()
        if not (cfg.auto_resume and cfg.enabled):
            return

        async def _resume() -> None:
            await asyncio.sleep(3)  # let the loop and Telethon settle first
            try:
                await self.start_listening()
                self.log("info", "Listener auto-resumed after restart")
            except Exception as exc:
                self.log("error", "Auto-resume failed: {}".format(exc))

        asyncio.run_coroutine_threadsafe(_resume(), self.loop)

    # ------------------------------------------------------------------
    # signal handling
    # ------------------------------------------------------------------

    async def _handle_message(self, event) -> None:
        cfg = config.load()
        text = event.message.message or ""
        chat_id = str(cfg.tg_chat_id)
        message_id = int(event.message.id)

        if not text.strip():
            return

        # Parse first so the stored row explains itself, then let the UNIQUE
        # constraint on (chat_id, message_id) decide whether we act.
        try:
            signal = parse_signal(text, cfg.default_duration, cfg.default_duration_unit)
        except ParseError as exc:
            # Separate the group's own result posts from messages the parser
            # genuinely failed on, so a real regression stands out.
            if looks_like_result(text):
                status, reason = "result", "trade result posted by the group"
            else:
                status, reason = "ignored", str(exc)
            db.insert_signal(
                chat_id, message_id, text, False, None, None, None, None,
                status, reason,
            )
            return

        signal_id = db.insert_signal(
            chat_id, message_id, text, True, signal.pair, signal.direction,
            signal.duration, signal.duration_unit, "received", None,
        )
        if signal_id is None:
            self.log("warning", "Duplicate message {} ignored".format(message_id))
            return

        self.status["last_signal_at"] = time.time()
        self.log("info", "Signal: {} {} {}{}".format(
            signal.pair, direction_label(signal.direction),
            signal.duration, signal.duration_unit))

        blocked = self._check_guards(cfg, event, signal)
        if blocked:
            db.update_signal_status(signal_id, "skipped", blocked)
            self.log("warning", "Skipped {}: {}".format(signal.pair, blocked))
            return

        deriv_symbol = symbols.resolve(signal.pair)
        if not deriv_symbol:
            reason = "{} is not tradable on Deriv".format(signal.pair)
            db.update_signal_status(signal_id, "skipped", reason)
            self.log("warning", reason)
            return

        await self._execute(cfg, signal_id, signal, deriv_symbol)

    def _check_guards(self, cfg: config.Config, event, signal) -> Optional[str]:
        """Return a reason string when the trade must not be placed."""
        # Stale signals — e.g. history replayed after a reconnect.
        try:
            age = time.time() - event.message.date.timestamp()
        except Exception:
            age = 0
        if cfg.max_signal_age_seconds and age > cfg.max_signal_age_seconds:
            return "signal is {:.0f}s old (limit {}s)".format(
                age, cfg.max_signal_age_seconds)

        seconds = duration_to_seconds(signal.duration, signal.duration_unit)
        if seconds < cfg.min_duration_minutes * 60:
            return "expiry {}{} below minimum {}m".format(
                signal.duration, signal.duration_unit, cfg.min_duration_minutes)
        if seconds > cfg.max_duration_minutes * 60:
            return "expiry {}{} above maximum {}m".format(
                signal.duration, signal.duration_unit, cfg.max_duration_minutes)

        today = db.trades_since(_day_start_ts())
        if cfg.max_trades_per_day and len(today) >= cfg.max_trades_per_day:
            return "daily trade cap reached ({})".format(cfg.max_trades_per_day)

        if cfg.max_daily_loss:
            pnl = sum(t["profit"] or 0.0 for t in today)
            if pnl <= -abs(cfg.max_daily_loss):
                return "daily loss limit hit ({:.2f})".format(pnl)

        if cfg.max_concurrent_trades:
            if len(db.open_trades()) >= cfg.max_concurrent_trades:
                return "max concurrent trades reached ({})".format(
                    cfg.max_concurrent_trades)

        return None

    def _capacity_reason(self, cfg: config.Config) -> Optional[str]:
        """Caps that shift as contracts are opened, re-checked between each.

        Without this, asking for 5 contracts would sail past a 3-open limit.
        """
        if cfg.max_trades_per_day:
            if len(db.trades_since(_day_start_ts())) >= cfg.max_trades_per_day:
                return "daily trade cap reached ({})".format(cfg.max_trades_per_day)
        if cfg.max_concurrent_trades:
            if len(db.open_trades()) >= cfg.max_concurrent_trades:
                return "max concurrent trades reached ({})".format(
                    cfg.max_concurrent_trades)
        return None

    async def _execute(
        self, cfg: config.Config, signal_id: int, signal, deriv_symbol: str
    ) -> Dict[str, Any]:
        """Open the configured number of contracts for one signal.

        Each is a separate contract at the full stake, placed and settled
        independently, so a partial fill is a normal outcome rather than a
        failure — the caps are what stop a burst from over-committing.

        Returns a summary so callers can report what actually happened. A
        failure here is recorded rather than raised, so anything reporting
        to a user must check `placed` instead of assuming success.
        """
        wanted = max(1, min(int(cfg.contracts_per_signal or 1), 5))
        result = {
            "symbol": deriv_symbol, "wanted": wanted, "placed": 0,
            "mode": cfg.mode, "error": None, "contract_ids": [],
        }

        for index in range(wanted):
            blocked = self._capacity_reason(cfg) if index else None
            if blocked:
                result["error"] = blocked
                self.log("warning", "Placed {} of {} contracts for {}: {}".format(
                    result["placed"], wanted, deriv_symbol, blocked))
                break

            failure = await self._place_one(cfg, signal_id, signal, deriv_symbol,
                                            index + 1, wanted)
            if failure:
                result["error"] = failure
                break
            result["placed"] += 1

        placed = result["placed"]
        if placed == 0:
            db.update_signal_status(
                signal_id, "error", result["error"] or "no contracts opened")
        elif placed < wanted:
            db.update_signal_status(
                signal_id, "executed", "{} of {} contracts".format(placed, wanted))
        else:
            db.update_signal_status(signal_id, "executed")

        result["contract_ids"] = [
            row["contract_id"] for row in db.recent_trades(placed)
            if row["signal_id"] == signal_id and row["contract_id"]
        ]
        return result

    async def _place_one(
        self, cfg: config.Config, signal_id: int, signal, deriv_symbol: str,
        index: int, total: int,
    ) -> Optional[str]:
        """Open a single contract. Returns None on success, else the reason."""
        trade_id = db.insert_trade(
            signal_id=signal_id,
            mode=cfg.mode,
            deriv_symbol=deriv_symbol,
            contract_type=signal.direction,
            stake=cfg.stake,
            currency=cfg.deriv_currency,
            duration=signal.duration,
            duration_unit=signal.duration_unit,
            status="pending",
        )

        try:
            client = await self._deriv_client()

            if cfg.is_live:
                result = await client.place_trade(
                    symbol=deriv_symbol,
                    contract_type=signal.direction,
                    stake=cfg.stake,
                    currency=cfg.deriv_currency,
                    duration=signal.duration,
                    duration_unit=signal.duration_unit,
                )
                db.update_trade(
                    trade_id,
                    contract_id=result["contract_id"],
                    buy_price=result["buy_price"],
                    payout=result["payout"],
                    longcode=result["longcode"],
                    status="open",
                )
                self.log("info", "LIVE bought {}{} {} {}{} stake {:.2f} -> contract {}".format(
                    deriv_symbol, _of(index, total), direction_label(signal.direction),
                    signal.duration, signal.duration_unit, cfg.stake,
                    result["contract_id"]))
            else:
                # Paper: price it for real (free) so invalid contracts still
                # fail here, then track the spot to settle it honestly.
                prop = await client.proposal(
                    symbol=deriv_symbol,
                    contract_type=signal.direction,
                    amount=cfg.stake,
                    currency=cfg.deriv_currency,
                    duration=signal.duration,
                    duration_unit=signal.duration_unit,
                )
                entry = await self.spot(deriv_symbol)
                db.update_trade(
                    trade_id,
                    payout=prop.get("payout"),
                    longcode=prop.get("longcode", ""),
                    entry_spot=entry,
                    status="open",
                )
                self.log("info", "PAPER {}{} {} {}{} stake {:.2f} entry {}".format(
                    deriv_symbol, _of(index, total), direction_label(signal.direction),
                    signal.duration, signal.duration_unit, cfg.stake, entry))

        except (DerivError, ConnectionError, TimeoutError, RuntimeError) as exc:
            db.update_trade(trade_id, status="error", error=str(exc))
            self.log("error", "Execution failed for {}{}: {}".format(
                deriv_symbol, _of(index, total), exc))
            return str(exc)

        return None

    # ------------------------------------------------------------------
    # settlement
    # ------------------------------------------------------------------

    async def _settle_loop(self) -> None:
        while True:
            try:
                await self._settle_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("warning", "settle loop: {}".format(exc))
            await asyncio.sleep(SETTLE_POLL_SECONDS)

    async def _settle_once(self) -> None:
        rows = db.open_trades()
        if not rows:
            return
        for row in rows:
            if row["mode"] == "live":
                await self._settle_live(row)
            else:
                await self._settle_paper(row)

    async def _settle_live(self, row) -> None:
        if not row["contract_id"]:
            return
        try:
            client = await self._deriv_client()
            contract = await client.contract_status(row["contract_id"])
        except Exception as exc:
            self.log("warning", "status check failed for {}: {}".format(
                row["contract_id"], exc))
            return

        if not contract.get("is_sold"):
            return

        profit = float(contract.get("profit", 0.0))
        db.update_trade(
            row["id"],
            status="won" if profit > 0 else "lost",
            profit=profit,
            entry_spot=contract.get("entry_spot"),
            exit_spot=contract.get("exit_spot") or contract.get("current_spot"),
            settled_at=time.time(),
        )
        self.log("info", "Settled {} {}: {:.2f}".format(
            row["deriv_symbol"], "WON" if profit > 0 else "LOST", profit))

    async def _settle_paper(self, row) -> None:
        expires_at = row["created_at"] + duration_to_seconds(
            row["duration"], row["duration_unit"]
        )
        if time.time() < expires_at:
            return

        entry = row["entry_spot"]
        exit_spot = await self.spot(row["deriv_symbol"])
        if entry is None or exit_spot is None:
            db.update_trade(
                row["id"], status="error", error="no spot data to settle paper trade",
                settled_at=time.time(),
            )
            return

        # Deriv Rise/Fall: an unchanged price loses.
        won = exit_spot > entry if row["contract_type"] == "CALL" else exit_spot < entry
        payout = row["payout"] or 0.0
        profit = (payout - row["stake"]) if won else -row["stake"]
        db.update_trade(
            row["id"],
            status="won" if won else "lost",
            profit=profit,
            exit_spot=exit_spot,
            settled_at=time.time(),
        )
        self.log("info", "PAPER settled {} {}: {:.2f}".format(
            row["deriv_symbol"], "WON" if won else "LOST", profit))

    # ------------------------------------------------------------------
    # manual test order
    # ------------------------------------------------------------------

    async def test_trade(
        self, pair: str, direction: str, duration: int, unit: str
    ) -> Dict[str, Any]:
        """Place one trade by hand, honouring the current paper/live mode."""
        cfg = config.load()
        deriv_symbol = symbols.resolve(pair)
        if not deriv_symbol:
            raise RuntimeError("{} is not tradable on Deriv".format(pair))

        signal_id = db.insert_signal(
            "manual", int(time.time() * 1000), "manual test: {} {} {}{}".format(
                pair, direction, duration, unit),
            True, pair, direction, duration, unit, "received", "manual",
        )

        class _Sig:
            pass

        sig = _Sig()
        sig.pair, sig.direction = pair, direction
        sig.duration, sig.duration_unit = duration, unit

        return await self._execute(cfg, signal_id, sig, deriv_symbol)


_engine_lock = threading.Lock()
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """Process-wide singleton. Streamlit also caches this, but the lock keeps
    it safe if two sessions race on first load."""
    global _engine
    with _engine_lock:
        if _engine is None:
            db.init()
            _engine = Engine()
            _engine.maybe_auto_resume()
        return _engine
