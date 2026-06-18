"""
Gold Mean-Reversion Trading Bot for Capital.com (RSI-based)
=============================================================

This is the SHORTEST-TIMEFRAME strategy of the three discussed:
mean-reversion using RSI on Gold, running on 15-minute candles.

LOGIC:
  - RSI < 30  -> oversold  -> open LONG  (if no position is open)
  - RSI > 70  -> overbought -> open SHORT (if no position is open)
  - RSI drifts back to 45-55 while in a position -> close it
  - Every order carries a fixed stop loss / take profit for risk control

SETUP BEFORE RUNNING:
  1. pip install requests
  2. Create a Capital.com DEMO account, enable 2FA, then generate an API
     key (in the platform: Settings > API integrations).
  3. Set these as environment variables (never hardcode secrets in code):
       CAPITAL_API_KEY
       CAPITAL_IDENTIFIER   (your login email)
       CAPITAL_PASSWORD
  4. Run:  python gold_mean_reversion_bot.py --find-epic
     This searches Capital.com's market list for "gold" and prints the
     exact market codes (epics) available. Copy the correct one into
     GOLD_EPIC below (commonly "GOLD", but confirm it yourself).
  5. Re-check STOP_LOSS_POINTS / TAKE_PROFIT_POINTS against gold's actual
     recent volatility before running live (see note near those
     variables below) - the defaults here are placeholders, not advice.
  6. Run:  python gold_mean_reversion_bot.py
     This starts the bot on your DEMO account only.

This script targets the DEMO base URL. Do not point it at the live URL
until you've reviewed weeks of demo performance, understand the field
names returned by your account's API responses (verify against the
Capital.com Postman collection), and accept the risks of automated
leveraged trading with real money.
"""

import os
import sys
import time
import csv
import requests
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"

API_KEY = os.getenv("CAPITAL_API_KEY")
IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
PASSWORD = os.getenv("CAPITAL_PASSWORD")

GOLD_EPIC = "GOLD"          # confirm/replace by running --find-epic first
RESOLUTION = "MINUTE_15"    # 15-minute candles, the shortest of our 3 strategies
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_EXIT_BAND = (45, 55)    # close the position once RSI drifts back near 50

POSITION_SIZE = 0.1         # smallest reasonable size for demo testing

# Stop loss / take profit are now sized off ATR (Average True Range)
# instead of a fixed dollar amount, so they automatically widen during
# volatile periods and tighten during calm ones, rather than getting
# clipped by normal noise on a quiet day or being too loose on a wild one.
ATR_PERIOD = 14
STOP_LOSS_ATR_MULTIPLIER = 1.5
TAKE_PROFIT_ATR_MULTIPLIER = 2.0

LOG_FILE = "gold_trades_log.csv"
HEARTBEAT_FILE = "gold_bot_heartbeat.csv"
# Note: scheduling is handled externally now (e.g. a GitHub Actions cron job
# every 15 minutes), not by an internal sleep loop. See run_once() below.

# ---------------------------------------------------------------------------
# SESSION HANDLING (tokens expire after 10 min of inactivity)
# ---------------------------------------------------------------------------

class CapitalSession:
    def __init__(self):
        self.cst = None
        self.security_token = None
        self.last_auth_time = 0

    def authenticate(self):
        if not all([API_KEY, IDENTIFIER, PASSWORD]):
            sys.exit("Missing CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD env vars.")
        resp = requests.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={"identifier": IDENTIFIER, "password": PASSWORD, "encryptedPassword": False},
        )
        resp.raise_for_status()
        self.cst = resp.headers["CST"]
        self.security_token = resp.headers["X-SECURITY-TOKEN"]
        self.last_auth_time = time.time()
        print(f"[{datetime.now()}] Authenticated successfully.")

    def headers(self):
        if time.time() - self.last_auth_time > 8 * 60:   # refresh proactively
            self.authenticate()
        return {
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token,
            "Content-Type": "application/json",
        }


session = CapitalSession()

# ---------------------------------------------------------------------------
# MARKET / EPIC DISCOVERY  (run once with --find-epic)
# ---------------------------------------------------------------------------

def find_gold_epic():
    session.authenticate()
    resp = requests.get(f"{BASE_URL}/markets", headers=session.headers(),
                         params={"searchTerm": "gold"})
    resp.raise_for_status()
    markets = resp.json().get("markets", [])
    if not markets:
        print("No markets found for 'gold'. Try searchTerm='XAU' instead.")
        return
    print("Matching markets:")
    for m in markets:
        print(f"  epic={m.get('epic'):<15} name={m.get('instrumentName')}")
    print("\nSet GOLD_EPIC in this script to whichever epic matches spot gold.")

# ---------------------------------------------------------------------------
# PRICE DATA + RSI
# ---------------------------------------------------------------------------

def get_recent_bars(num_bars=100):
    resp = requests.get(
        f"{BASE_URL}/prices/{GOLD_EPIC}",
        headers=session.headers(),
        params={"resolution": RESOLUTION, "max": num_bars},
    )
    resp.raise_for_status()
    data = resp.json().get("prices", [])
    highs = [(p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2 for p in data]
    lows = [(p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2 for p in data]
    closes = [(p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2 for p in data]
    return highs, lows, closes


def calculate_atr(highs, lows, closes, period=ATR_PERIOD):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def calculate_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[-i] - closes[-i - 1]
        (gains if change > 0 else losses).append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------

def get_open_position():
    resp = requests.get(f"{BASE_URL}/positions", headers=session.headers())
    resp.raise_for_status()
    for pos in resp.json().get("positions", []):
        if pos["market"]["epic"] == GOLD_EPIC:
            return pos
    return None


def open_position(direction, current_price, atr):
    stop_distance = atr * STOP_LOSS_ATR_MULTIPLIER
    profit_distance = atr * TAKE_PROFIT_ATR_MULTIPLIER
    if direction == "BUY":
        stop_level = current_price - stop_distance
        profit_level = current_price + profit_distance
    else:
        stop_level = current_price + stop_distance
        profit_level = current_price - profit_distance

    payload = {
        "epic": GOLD_EPIC,
        "direction": direction,
        "size": POSITION_SIZE,
        "stopLevel": round(stop_level, 2),
        "profitLevel": round(profit_level, 2),
    }
    resp = requests.post(f"{BASE_URL}/positions", headers=session.headers(), json=payload)
    resp.raise_for_status()
    deal_ref = resp.json().get("dealReference")
    print(f"[{datetime.now()}] Opened {direction} at ~{current_price:.2f} "
          f"(stop={round(stop_level,2)}, profit={round(profit_level,2)}, ATR={round(atr,2)}) (ref {deal_ref})")
    log_trade(direction, current_price, "OPEN")
    return deal_ref


def close_position(position):
    deal_id = position["position"]["dealId"]
    resp = requests.delete(f"{BASE_URL}/positions/{deal_id}", headers=session.headers())
    resp.raise_for_status()
    print(f"[{datetime.now()}] Closed position {deal_id}")
    log_trade(position["position"]["direction"], position["position"]["level"], "CLOSE")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def log_trade(direction, price, action):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "action", "direction", "price"])
        writer.writerow([datetime.now().isoformat(), action, direction, price])


def write_heartbeat(price, rsi, position):
    """Logs a status line on EVERY run, regardless of whether a trade
    fired, so you have visible proof the bot is checking correctly even
    during long stretches with no trade signal."""
    file_exists = os.path.isfile(HEARTBEAT_FILE)
    with open(HEARTBEAT_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "price", "rsi", "position_open"])
        writer.writerow([
            datetime.now().isoformat(),
            round(price, 2) if price is not None else "",
            round(rsi, 2) if rsi is not None else "",
            "yes" if position else "no",
        ])

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def run_once():
    """
    Performs ONE check-and-act cycle, then returns.

    This is intentionally NOT an infinite loop. Each GitHub Actions run
    spins up a fresh, short-lived container, so the scheduling (every 15
    minutes) is handled by the workflow's cron trigger, not by sleeping
    inside this script. Position state lives on Capital.com's side
    (queried fresh via get_open_position() each run), so there's no
    issue with statelessness between runs - only the CSV trade log needs
    to persist, which the GitHub Actions workflow handles by committing
    it back to the repo after each run.
    """
    session.authenticate()
    try:
        highs, lows, closes = get_recent_bars()
        rsi = calculate_rsi(closes)
        atr = calculate_atr(highs, lows, closes)
        current_price = closes[-1]
        position = get_open_position()

        print(f"[{datetime.now()}] Price={current_price:.2f}  RSI={rsi}  ATR={atr}")
        write_heartbeat(current_price, rsi, position)

        if position is None:
            if atr is not None and rsi is not None and rsi < RSI_OVERSOLD:
                open_position("BUY", current_price, atr)
            elif atr is not None and rsi is not None and rsi > RSI_OVERBOUGHT:
                open_position("SELL", current_price, atr)
        else:
            if rsi is not None and RSI_EXIT_BAND[0] <= rsi <= RSI_EXIT_BAND[1]:
                close_position(position)

    except requests.HTTPError as e:
        print(f"[{datetime.now()}] API error: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")


if __name__ == "__main__":
    if "--find-epic" in sys.argv:
        find_gold_epic()
    else:
        run_once()
