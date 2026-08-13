# Telegram → Deriv Signal Copier

Reads trade signals from a Telegram group (`AUDCHF 15 minutes Down`) and places
the matching Rise/Fall binary option on your Deriv account. Streamlit UI,
local SQLite storage, no external services.

**It trades live.** Every parsed signal places a real order on the connected
Deriv account. There is no switch in the UI — point it at a demo account until
you trust it. Paper mode still exists for dry runs, from the command line:

```bash
./.venv/bin/python -m copier.mode paper   # parse and price, send nothing
./.venv/bin/python -m copier.mode live    # resume trading
```

---

## Run it

```bash
cd "~/Desktop/AI projects/Telegram copier "
./.venv/bin/streamlit run app.py
```

Then open http://localhost:8501.

The virtualenv is already set up. To rebuild it from scratch:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

---

## First-time setup

Everything below happens on the **Connections** page.

### 1. Telegram

You need an `api_id` and `api_hash`. These identify the *application*, not your
account, and you only get them once:

1. Go to https://my.telegram.org and log in with your phone number.
2. Open **API development tools**, create an app (any name, e.g. "copier").
3. Copy the `api_id` and `api_hash` into the app and save.

Then enter your phone number in international format (e.g. `+27821234567`),
press **Send login code**, and type in the code Telegram sends you. If you have
two-step verification enabled, you'll be asked for that password too.

This signs in as *you* — a user session, not a bot. That matters because bots
can't read messages in groups they don't administer, and you almost certainly
don't administer the signal group.

The session is stored in `session/copier.session`. You log in once; it survives
restarts. **Anyone with that file can act as your Telegram account** — it is
gitignored, keep it that way.

### 2. Pick the group

Press **Load my groups**, choose your signal group (`AI signal / VIP`) from the
dropdown, and press **Use this group**.

### 3. Check the parser before trusting it

Press **Test parser on recent messages**. This pulls the last 25 messages and
shows how each would be interpreted — without trading any of them. Read that
table carefully. If a real signal shows `—`, the parser missed it and needs a
rule added; if chatter is being read as a signal, that's worse. This step is
the whole point of the setup, don't skip it.

### 4. Deriv

Create an API token at Deriv → **Settings → API token**, with the **Read**,
**Trade** and **Trading information** scopes. Paste it in and press
**Save & connect** — the app will show which account it landed on and whether
that account is demo or real.

Deriv issues **two different token formats, and they are not interchangeable**:

| Format | Where from | How it authenticates |
|---|---|---|
| `a1-…` (~32 chars) | app.deriv.com → Settings → API token | Legacy WebSocket `authorize` — what this app currently uses |
| `pat_…` (68 chars) | developers.deriv.com → API tokens | Current REST API: HTTP Bearer, then an OTP exchange for a WebSocket |

A `pat_` token sent to the legacy `authorize` call is rejected with
`InvalidToken`, which is confusing but expected — it's not a bad token, it's
the wrong protocol for it.

**The app auto-detects which you pasted** from its prefix and uses the matching
flow, so either works. With a `pat_` token it lists your Options accounts and
connects to one; leave **Prefer the demo account** ticked until you've proven
the setup.

### A `pat_` token also needs a registered app

The current API rejects the legacy public app ids. Every request carries a
`Deriv-App-ID` header, and it must be the id of an app registered on the new
platform:

1. developers.deriv.com → **Registered apps** → create an app.
   Scope **Trade** only. Redirect URL is unused by the PAT flow but the form
   demands one, and it refuses `localhost`.
2. Copy the app's **id** into the `app_id` field — not its name, and not the
   token's name. Current ids are long alphanumeric strings such as
   `k7Jd2Xq8mNpR4vTwYzB6c`; legacy apps used plain numbers.

Without it Deriv answers `401 Invalid application`, even though the token
itself is perfectly good. The two failures are easy to tell apart:

| Response | Meaning |
|---|---|
| `Invalid application` | Token is fine, **app_id** is wrong or unregistered |
| `Invalid or expired token` | app_id is fine, **token** is wrong |
| `Deriv-App-ID header is required for PAT tokens` | app_id field is empty |

To see exactly what the current API says about your login:

```bash
./.venv/bin/python tools/probe_new_api.py
```

If a token is rejected and you can't see why:

```bash
./.venv/bin/python tools/check_deriv.py
```

That checks the token exactly as the app stored it, reports its shape, tries
both Deriv endpoints, and lists the token's scopes if it authorizes. It never
prints the token in full.

Use a **demo account token first.** The token is what decides whether real money
moves; there is no separate switch on Deriv's side.

Then press **Refresh tradable symbols** so the app knows exactly what Deriv
will accept and can reject unknown pairs before spending anything.

> A valid token is required even in paper mode — Deriv requires authorization
> to price a contract, and paper trades are priced for real so payout figures
> are honest.

---

## How a signal becomes a trade

```
Telegram message
      │
      ▼
  parse  ──► not a signal?  ──► logged as "ignored", nothing happens
      │
      ▼
  dedupe on (chat_id, message_id)  ──► already seen? ──► dropped
      │
      ▼
  guard rails: age, expiry band, daily cap, loss limit, open positions
      │
      ▼
  map pair → Deriv symbol   (AUDCHF → frxAUDCHF)
      │
      ├── paper: price it, record the entry spot, settle at expiry from ticks
      └── live:  proposal → buy, then poll until Deriv settles it
```

Direction maps to Deriv's Rise/Fall contracts: **Up → CALL (Rise)**,
**Down → PUT (Fall)**.

### What the parser accepts

| Message | Read as |
|---|---|
| `AUDCHF 15 minutes Down` | frxAUDCHF, Fall, 15m |
| `EURGBP 15 minutes Up` | frxEURGBP, Rise, 15m |
| `AUD/CHF 15 min UP` | frxAUDCHF, Rise, 15m |
| `🔴 EURUSD 15 minutes` | frxEURUSD, Fall, 15m |
| `USDJPY 5 ticks call` | frxUSDJPY, Rise, 5 ticks |
| `AUDCHF DOWN` | frxAUDCHF, Fall, *fallback expiry* |

It refuses anything ambiguous — no direction, no recognisable pair, or both
"up" and "down" in one message. A skipped trade costs nothing; a trade in the
wrong direction costs the stake.

Expiry comes from the signal itself. The fallback on the *Trading rules* page
is used only when a message states no duration.

---

## Guard rails

All on the *Trading rules* page. They're checked before every order.

| Setting | Default | Does |
|---|---|---|
| Stake | 1.00 | Fixed amount per **contract** |
| Contracts per signal | 1 | Opens 1–5 separate contracts on the asset, each at the full stake |
| Min / max expiry | 1m / 240m | Rejects durations outside the band |
| Max trades per day | 20 | Counts from UTC midnight; 0 = unlimited |
| Max daily loss | 50.00 | Once today's P/L drops below this, new signals are skipped |
| Max open trades | 3 | Caps simultaneous exposure |
| Ignore signals older than | 90s | Stops old messages being replayed after a reconnect |

### Contracts per signal

Set it above 1 and each signal opens that many independent contracts on the
same asset, each staking the full amount — `3 × 50.00` puts **150.00** at risk
per signal, not 50.00 split three ways. They are priced, filled and settled
separately, so they can return different payouts.

Each contract counts as one trade against **max trades per day** and **max open
trades**. If a cap is reached partway, the remaining contracts are dropped and
the signal is recorded as e.g. *"2 of 5 contracts"* rather than failing. Keep
**max open trades** at least as large as this setting, or every signal will be
cut short.

By default the listener never starts itself: after a restart you press **Start
listener** again, so nothing resumes live trading unattended. On an always-on
server that's the wrong default — tick **Auto-resume the listener when the app
restarts** on the Dashboard and it comes back by itself after a reboot or
crash. Stopping it deliberately still keeps it stopped.

---

## Before pointing it at real money

The app trades live from the moment you start the listener, so the account you
connect is the only thing standing between a parser mistake and your balance.

1. Connect a **demo** account token and let it run. Real signals, real orders,
   demo funds.
2. Check *History* — win rate, P/L curve, and any errors.
3. Only then connect a real-money token.

The sidebar always shows the account id and whether it is DEMO or REAL. To dry
run without any orders at all, `python -m copier.mode paper`, restart, and
watch the *Signals* tab.

---

## Tests

```bash
./.venv/bin/python tests/test_parser.py   # 19 message-parsing cases
./.venv/bin/python tests/test_engine.py   # 28 checks: dedupe, guards, settlement
```

The engine tests use a stubbed Deriv client, so they never touch the network or
place an order.

---

## Files

```
app.py                  Streamlit UI (4 pages)
copier/parser.py        message text  → Signal
copier/symbols.py       pair          → Deriv symbol
copier/deriv.py         Deriv WebSocket API client
copier/engine.py        background thread: Telegram listener, execution, settlement
copier/db.py            SQLite schema and queries
copier/config.py        settings, defaults, env-var overrides
tools/check_deriv.py    diagnose a rejected Deriv token
data/copier.db          your database (gitignored)
session/                Telegram session (gitignored)
```

Secrets live in the `settings` table of `data/copier.db`, which is created
`0600`. If you'd rather not keep them there, set `DERIV_TOKEN`, `TG_API_ID` and
`TG_API_HASH` as environment variables — they override the stored values.

---

## Running on Streamlit Community Cloud

The deployed app clones the repo, and the repo deliberately contains no
`data/` or `session/` directory — so it starts with no Deriv token and no
Telegram login. Credentials have to arrive as **secrets** instead.

### Be clear about what this gets you

| Works | Doesn't |
|---|---|
| Dashboard reachable from anywhere | Listener stops when the app sleeps |
| Connects to Deriv, shows balance | Trade history wiped on every restart |
| Manual trades via *Try a signal* | **Dedupe state wiped — a replayed message can trade twice** |
| Reads the group, parses signals | Guard-rail counters (daily cap, loss limit) reset with it |

Streamlit Cloud suspends idle apps and gives each one an ephemeral disk. It
suits a dashboard you open occasionally; it cannot be relied on to copy
signals unattended. For that, use a VPS.

### 1. Generate a session string

`session/copier.session` cannot persist there, so export the login:

```bash
./.venv/bin/python tools/make_secrets.py
```

This collects everything already working locally — Deriv token, app id,
account, Telegram credentials and chat — and exports your existing login as a
portable session string. Nothing is re-authorised and no secret is printed to
the terminal; it writes `.streamlit/secrets.toml` (mode 0600, gitignored).

**`TG_SESSION_STRING` is a login credential for your Telegram account.** Treat
it like a password; revoke it from Telegram → Settings → Devices if it leaks.

### 2. Add the secrets

Open `.streamlit/secrets.toml`, copy all of it, and paste into
App → **⋮ → Settings → Secrets**. It looks like:

```toml
DERIV_TOKEN       = "pat_…"
DERIV_APP_ID      = "your-registered-app-id"
DERIV_ACCOUNT_ID  = "DOT…"          # which Options account to trade
TG_API_ID         = "1234567"
TG_API_HASH       = "…"
TG_CHAT_ID        = "-100…"
TG_CHAT_TITLE     = "AI signal / VIP"
TG_SESSION_STRING = "…"
```

The same file also configures a local run, so you can test it before deploying.

Environment variables of the same names work identically anywhere else.
Anything supplied this way overrides the database, so the app configures
itself on a disk it cannot write to.

### 3. Restart the app

Streamlit reboots it when secrets are saved. The sidebar should show your
Deriv account; press **Start listener** — and remember it stops again when the
app sleeps.

## Running on a VPS

A laptop sleeps, changes network and reboots for updates, so signals get
missed. For unattended running use a small always-on Linux box.

> **The dashboard has no login.** Never publish port 8501. The service binds to
> `127.0.0.1` and you reach it over an SSH tunnel. Anyone who could open that
> port could place trades on your account and read your API token.

### 1. Copy the project across

From your Mac — excluding the virtualenv, which must be rebuilt for Linux:

```bash
cd ~/Desktop
rsync -av --exclude '.venv' --exclude '__pycache__' \
    "AI projects/Telegram copier /" root@YOUR_VPS_IP:/tmp/copier-src/
```

`data/` and `session/` come along too, which carries your Deriv token and
Telegram login. If you'd rather start clean, add `--exclude data --exclude
session` and re-enter both on the server.

### 2. Install

```bash
ssh root@YOUR_VPS_IP
cd /tmp/copier-src && sudo bash deploy/install.sh
```

That creates a `copier` service user, installs to `/opt/copier`, builds the
virtualenv, and installs and starts a systemd service that restarts on crash
and survives reboots.

### 3. Open the dashboard

From your own machine:

```bash
ssh -N -L 8501:127.0.0.1:8501 root@YOUR_VPS_IP
```

Leave that running and open <http://localhost:8501>.

### 4. Finish setup on the server

- **Telegram** — if you copied `session/`, you're still signed in. If Telegram
  invalidated it after the IP change, just sign in again on the Connections
  page.
- **Auto-resume** — tick **Auto-resume the listener when the app restarts** on
  the Dashboard. Without it the listener stays stopped after a reboot, which
  is the right default on a desktop and the wrong one on a server.
- Press **Start listener**.

### Day to day

```bash
sudo systemctl status copier      # running?
sudo systemctl restart copier     # after a code change
sudo journalctl -u copier -f      # live logs
```

To deploy a change, re-run steps 1 and 2 — `install.sh` never overwrites
`data/` or `session/`.

## Notes and limits

- **The app must stay running** to catch signals. Closing the terminal stops
  the listener. For unattended running, use `tmux`/`screen` or a launchd job.
- **Paper settlement is approximate.** It compares the spot at entry against
  the spot at expiry, sampled by polling every 5 seconds, whereas Deriv settles
  on the exact tick. Expect small differences on marginal trades.
- **Forex markets close on weekends.** Deriv rejects those orders and the app
  records the rejection rather than retrying. Synthetic indices (`R_100`) trade
  continuously.
- **Deriv enforces its own minimums** on stake and duration, and they vary by
  symbol. A rejected order appears in the log with Deriv's reason.
- **The two APIs differ in more than authentication.** Verified against a live
  demo account, the current (PAT) socket:
  - names the instrument `underlying_symbol`; sending `symbol` is rejected
    with *"Properties not allowed: symbol"*
  - rejects `product_type` on `active_symbols`
  - returns `underlying_symbol` / `underlying_symbol_name` in `active_symbols`
    rather than `symbol` / `display_name`
  - is otherwise compatible: `ping`, `balance`, `portfolio`, `proposal`,
    `ticks_history` and `proposal_open_contract` all behave as before, and
    symbol names are unchanged (`frxAUDCHF`, `1HZ100V`, …)

  `copier/deriv.py` switches these per token type, so both paths work.
- Binary options are high risk. Copying signals automatically means losses
  arrive automatically too — the daily loss limit is the setting worth getting
  right.
