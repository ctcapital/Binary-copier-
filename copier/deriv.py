"""Async client for the Deriv WebSocket API (v3).

One connection is shared by the whole app. A single reader task routes replies
back to per-request futures by `req_id`, so concurrent calls (buying one
contract while polling another) never read each other's responses.
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import websockets

from . import deriv_rest

DEFAULT_ENDPOINT = "wss://ws.derivws.com/websockets/v3"
# 1089 is Deriv's public app id. Fine to start with; register your own at
# https://api.deriv.com if you want the traffic attributed to your app.
DEFAULT_APP_ID = "1089"

PING_INTERVAL = 30.0
REQUEST_TIMEOUT = 30.0


# Deriv issues two token formats: "pat_" + 64 hex from the current developer
# portal (developers.deriv.com), and the legacy "a1-" tokens. Both are valid.
DERIV_TOKEN_PREFIXES = ("pat_", "a1-")

# Prefixes belonging to other services, pasted in by mistake often enough to
# be worth naming. Note "github_pat_" is checked before the Deriv "pat_".
FOREIGN_TOKEN_PREFIXES = {
    "github_pat_": "a GitHub personal access token",
    "ghp_": "a GitHub personal access token",
    "gho_": "a GitHub OAuth token",
    "sk-": "an OpenAI or Anthropic API key",
    "xoxb-": "a Slack bot token",
    "xoxp-": "a Slack user token",
    "glpat-": "a GitLab personal access token",
    "AKIA": "an AWS access key id",
}


def token_warning(token: str) -> Optional[str]:
    """Flag a token that clearly can't be a Deriv one, before we call the API.

    Deliberately permissive: Deriv has changed token formats before, and
    refusing to even try a valid token is worse than a wasted round trip.
    """
    token = (token or "").strip()
    if not token:
        return None

    lowered = token.lower()
    for prefix, what in FOREIGN_TOKEN_PREFIXES.items():
        if lowered.startswith(prefix.lower()):
            return (
                "This looks like {}, not a Deriv API token. Create one at "
                "developers.deriv.com → API tokens.".format(what)
            )

    if lowered.startswith("pat_") and len(token) < 40:
        return (
            "This looks truncated — a `pat_` token from Deriv is 68 characters "
            "and this one is {}. It is shown only once, so if you refreshed the "
            "page you'll need to create a new one.".format(len(token))
        )

    if not any(lowered.startswith(p) for p in DERIV_TOKEN_PREFIXES):
        # Not recognised, but not obviously foreign either — let Deriv decide.
        return None

    return None


def _account_field(account: Dict[str, Any], names) -> Any:
    """Read the first present key. The PAT API's field names aren't pinned
    down in the docs, so accept the plausible spellings."""
    for name in names:
        if account.get(name) not in (None, ""):
            return account[name]
    return None


def _account_is_demo(account: Dict[str, Any]) -> bool:
    for key in ("is_virtual", "is_demo", "demo"):
        if key in account:
            return bool(account[key])
    kind = str(_account_field(account, ("type", "account_type", "category")) or "")
    if kind:
        return kind.lower() in ("demo", "virtual")
    return str(_account_field(account, ("id", "account_id", "loginid")) or "") \
        .upper().startswith(("VR", "DEMO"))


class DerivError(Exception):
    """An `error` object came back from Deriv for one of our requests."""

    def __init__(self, code: str, message: str):
        super().__init__("{} ({})".format(message, code))
        self.code = code
        self.message = message


class DerivClient:
    def __init__(
        self,
        token: str,
        app_id: str = DEFAULT_APP_ID,
        endpoint: str = DEFAULT_ENDPOINT,
        logger=None,
        account_id: str = "",
        prefer_demo: bool = True,
    ):
        self.token = token
        self.app_id = str(app_id or DEFAULT_APP_ID)
        self.endpoint = endpoint
        self._log = logger or (lambda level, msg: None)

        # Which API this token belongs to. `pat_` tokens authenticate over REST
        # and get a short-lived OTP socket; `a1-` tokens use legacy authorize.
        self.is_pat = deriv_rest.is_pat(token)
        self.account_id = account_id
        self.prefer_demo = prefer_demo
        self.accounts: List[Dict[str, Any]] = []

        self._ws = None
        self._reader_task = None
        self._ping_task = None
        self._pending: Dict[int, "asyncio.Future"] = {}
        self._req_id = 0
        self._connect_lock = asyncio.Lock()

        self.account: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return "{}?app_id={}".format(self.endpoint, self.app_id)

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> Dict[str, Any]:
        """Open an authenticated socket. Returns a normalised account summary."""
        async with self._connect_lock:
            if self.connected and self.account:
                return self.account
            await self._teardown()
            if self.is_pat:
                await self._connect_pat()
            else:
                await self._connect_legacy()
            return self.account

    async def _open(self, url: str) -> None:
        self._ws = await websockets.connect(
            url, ping_interval=None, close_timeout=5, max_size=4 * 1024 * 1024
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._ping_task = asyncio.ensure_future(self._ping_loop())

    async def _connect_legacy(self) -> None:
        await self._open(self.url)
        auth = await self.authorize()
        self.account = {
            "loginid": auth.get("loginid", ""),
            "currency": auth.get("currency", ""),
            "is_virtual": bool(auth.get("is_virtual", 0)),
            "balance": auth.get("balance"),
            "api": "legacy",
        }

    async def _connect_pat(self) -> None:
        """REST: list accounts, exchange the PAT for an OTP socket URL.

        The OTP lasts 120 seconds and is single use, so this whole dance is
        repeated on every reconnect rather than cached.
        """
        loop = asyncio.get_event_loop()

        self.accounts = await loop.run_in_executor(
            None, deriv_rest.get_accounts, self.token, self.app_id
        )
        if not self.accounts:
            raise DerivError("NoAccounts",
                             "No Options trading accounts on this Deriv login")

        chosen = self._choose_account(self.accounts)
        account_id = _account_field(chosen, ("id", "account_id", "loginid"))
        if not account_id:
            raise DerivError("NoAccountId",
                             "Could not find an id on the account: {}".format(chosen))

        url = await loop.run_in_executor(
            None, deriv_rest.get_ws_url, self.token, self.app_id, account_id
        )
        await self._open(url)

        self.account = {
            "loginid": account_id,
            "currency": _account_field(chosen, ("currency", "currency_code")) or "USD",
            "is_virtual": _account_is_demo(chosen),
            "balance": _account_field(chosen, ("balance", "available_balance")),
            "api": "pat",
        }

    def _choose_account(self, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Explicit choice wins; otherwise prefer demo so mistakes are cheap."""
        if self.account_id:
            for account in accounts:
                if str(_account_field(
                        account, ("id", "account_id", "loginid"))) == str(self.account_id):
                    return account
        if self.prefer_demo:
            for account in accounts:
                if _account_is_demo(account):
                    return account
        return accounts[0]

    async def _teardown(self) -> None:
        for task in (self._reader_task, self._ping_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._ping_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        # Fail anything still waiting so callers don't hang on a dead socket.
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("Deriv connection closed"))
        self._pending.clear()

    async def close(self) -> None:
        async with self._connect_lock:
            self.account = {}
            await self._teardown()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                req_id = data.get("req_id")
                future = self._pending.pop(req_id, None) if req_id is not None else None
                if future is not None and not future.done():
                    future.set_result(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("warning", "Deriv socket dropped: {}".format(exc))
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("Deriv connection closed"))
            self._pending.clear()

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                try:
                    await self.send({"ping": 1}, timeout=10)
                except Exception:
                    return
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # request plumbing
    # ------------------------------------------------------------------

    async def send(self, payload: Dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> Dict[str, Any]:
        if self._ws is None or self._ws.closed:
            raise ConnectionError("not connected to Deriv")

        self._req_id += 1
        req_id = self._req_id
        message = dict(payload)
        message["req_id"] = req_id

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps(message))
            data = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError("Deriv did not answer {} in {}s".format(
                list(payload.keys())[0], timeout))
        finally:
            self._pending.pop(req_id, None)

        if "error" in data and data["error"]:
            err = data["error"]
            raise DerivError(err.get("code", "unknown"), err.get("message", "unknown error"))
        return data

    async def _ensure(self) -> None:
        """Reconnect and re-authorize if the socket has gone away."""
        if not self.connected or not self.account:
            await self.connect()

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def authorize(self) -> Dict[str, Any]:
        data = await self.send({"authorize": self.token})
        return data.get("authorize", {})

    async def balance(self) -> Dict[str, Any]:
        await self._ensure()
        data = await self.send({"balance": 1})
        return data.get("balance", {})

    async def active_symbols(self) -> List[Dict[str, Any]]:
        await self._ensure()
        request: Dict[str, Any] = {"active_symbols": "brief"}
        if not self.is_pat:
            # The current API rejects product_type outright.
            request["product_type"] = "basic"
        data = await self.send(request, timeout=45)
        return data.get("active_symbols", [])

    async def proposal(
        self,
        symbol: str,
        contract_type: str,
        amount: float,
        currency: str,
        duration: int,
        duration_unit: str,
    ) -> Dict[str, Any]:
        """Price a Rise/Fall contract. Raises DerivError if it isn't tradable."""
        await self._ensure()
        # The current API renamed this property; sending "symbol" is rejected
        # outright with "Properties not allowed: symbol".
        symbol_key = "underlying_symbol" if self.is_pat else "symbol"
        data = await self.send({
            "proposal": 1,
            symbol_key: symbol,
            "contract_type": contract_type,
            "amount": round(float(amount), 2),
            "basis": "stake",
            "currency": currency,
            "duration": int(duration),
            "duration_unit": duration_unit,
        })
        return data.get("proposal", {})

    async def buy(self, proposal_id: str, price: float) -> Dict[str, Any]:
        await self._ensure()
        data = await self.send({"buy": proposal_id, "price": round(float(price), 2)})
        return data.get("buy", {})

    async def contract_status(self, contract_id: str) -> Dict[str, Any]:
        await self._ensure()
        data = await self.send({
            "proposal_open_contract": 1,
            "contract_id": int(contract_id),
        })
        return data.get("proposal_open_contract", {})

    # ------------------------------------------------------------------
    # composed helper
    # ------------------------------------------------------------------

    async def place_trade(
        self,
        symbol: str,
        contract_type: str,
        stake: float,
        currency: str,
        duration: int,
        duration_unit: str,
    ) -> Dict[str, Any]:
        """Price then buy, returning the merged proposal + buy result.

        Deriv proposals expire quickly; if the price moves between the two
        calls we re-price once before giving up.
        """
        last_error = None
        for attempt in (1, 2):
            prop = await self.proposal(
                symbol, contract_type, stake, currency, duration, duration_unit
            )
            proposal_id = prop.get("id")
            if not proposal_id:
                raise DerivError("no_proposal", "Deriv returned no proposal id")
            try:
                bought = await self.buy(proposal_id, prop.get("ask_price", stake))
            except DerivError as exc:
                last_error = exc
                if attempt == 1 and exc.code in ("InvalidContractProposal", "PriceMoved"):
                    await asyncio.sleep(0.2)
                    continue
                raise
            return {
                "contract_id": str(bought.get("contract_id", "")),
                "buy_price": bought.get("buy_price"),
                "payout": bought.get("payout", prop.get("payout")),
                "longcode": bought.get("longcode", prop.get("longcode", "")),
                "start_time": bought.get("start_time", int(time.time())),
            }
        raise last_error if last_error else DerivError("buy_failed", "buy failed")


async def check_token(
    token: str, app_id: str = DEFAULT_APP_ID
) -> Dict[str, Any]:
    """One-shot credential check used by the Settings page."""
    client = DerivClient(token=token, app_id=app_id)
    try:
        account = await client.connect()
        try:
            balance = await client.balance()
            if balance.get("balance") is not None:
                account = dict(account, balance=balance["balance"])
        except (DerivError, ConnectionError, TimeoutError):
            pass  # balance needs the read scope; the connection is still good
        return account
    finally:
        await client.close()
