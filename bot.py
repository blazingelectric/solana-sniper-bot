import csv
import re
import time
from datetime import datetime
from typing import Iterable, List

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from flow_filters import FlowMode

# ---------- COLORS FOR TERMINAL OUTPUT ----------
RESET   = "\033[0m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
CYAN    = "\033[96m"
MAGENTA = "\033[95m"

# ----------------------------------
# CONFIG
# ----------------------------------

# Toggle between sniper mode (new pairs) and Cupsey-style flow mode (trending pairs)
FLOW_MODE = False  # set True to use Cupsey-style flow mode

# ---------- MARKET / DEX FILTERS ----------
# Only trade pairs on these AMM DEXes (tweak if you want more)
ALLOWED_DEXES = {"raydium", "orca", "meteora"}

# LP and age windows – aim for “just launched but not zero-info”
MIN_LIQ_USD   = 4000      # ignore anything with tiny LP
MAX_LIQ_USD   = 40000     # skip already very thick LP memes

MIN_AGE_SEC   = 60        # at least 1 min old
MAX_AGE_SEC   = 1800      # max 30 mins old (still “new”, but not birth-candle)

MIN_24H_SELLS = 30        # need a decent number of exits on the board
MIN_24H_VOL   = 20000     # minimum 24h volume in USD

NEW_PAIRS_URL = "https://dexscreener.com/new-pairs/solana?rankBy=pairAge&order=asc"
DEX_PAIR_API = "https://api.dexscreener.com/latest/dex/pairs/solana/{}"
TRENDING_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=sol"
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sniper-bot/1.0)"}
HTTP_TIMEOUT = 10

CHECK_INTERVAL_SECONDS = 10      # how often to poll the new-pairs page
MAX_PAIRS_PER_CYCLE    = 20      # don’t hammer the API too hard
LOG_FILE               = "meme_signals.csv"

# Honeypot heuristics / guardrails
MAX_TAX_PCT                = 15
NO_SELLS_AFTER_SECONDS     = 60
NO_SELLS_MIN_BUYS          = 10
EXTREME_BUY_PRESSURE_RATIO = 15
EXTREME_BUY_PRESSURE_BUYS  = 25
MIN_SELL_DELTA_WINDOW      = 5 * 60
MAX_SELL_DELTA             = 2
STILL_LOW_SELLS_WINDOW     = 10 * 60
STILL_LOW_SELLS_ALLOWANCE  = 3
HONEYPOT_FORCE_EXIT_AFTER  = 8 * 60

# ----------------------------------
# PAPER TRADE CONFIG
# ----------------------------------

PAPER_TRADE_SIZE_USD = 100          # pretend we snipe $100 per trade
TP_MULTIPLIER        = 1.3          # take profit at 1.3x (30% gain)
SL_MULTIPLIER        = 0.6          # stop loss at 0.6x (-40% loss)
MAX_HOLD_SECONDS     = 20 * 60      # close after 20 minutes if neither TP/SL hit

TRADES_LOG_FILE      = "paper_trades.csv"

# In-memory open trades: pairAddress -> dict
open_trades = {}

# Global HTTP session with retries so we are polite to the API and resilient to hiccups
def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


SESSION = build_http_session()
FLOW = FlowMode()


# ----------------------------------
# UTILS
# ----------------------------------

def safe_get(obj, path, default=None):
    """Safely dig into nested dicts via 'a.b.c' path."""
    cur = obj
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def compute_pair_age_sec(pair) -> int:
    """Return the pair age in seconds (non-negative)."""
    created_ms = safe_get(pair, "pairCreatedAt", None)
    if created_ms is None:
        return -1
    try:
        now_ms = int(time.time() * 1000)
        return max(0, int((now_ms - int(created_ms)) / 1000))
    except (TypeError, ValueError):
        return -1


def colour_for_score(score: int) -> str:
    """Return a colour code based on signal strength."""
    if score >= 85:
        return GREEN     # 🔥 top-tier
    elif score >= 75:
        return YELLOW    # ✅ strong
    elif score >= 60:
        return CYAN      # 👀 interesting
    else:
        return RESET


# ----------------------------------
# FETCH NEWEST PAIRS FROM WEBPAGE
# ----------------------------------

def fetch_new_pair_addresses(limit: int = MAX_PAIRS_PER_CYCLE) -> List[str]:
    """
    Scrape Dexscreener new Solana pairs page and extract newest pair addresses.
    We look for <a href="/solana/<pairAddress>"> links.
    """
    try:
        resp = SESSION.get(NEW_PAIRS_URL, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            print("❌ New-pairs page error:", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        addresses = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(r"^/solana/([A-Za-z0-9]{20,})$", href)
            if not m:
                continue
            addr = m.group(1)
            if addr in seen:
                continue
            seen.add(addr)
            addresses.append(addr)
            if len(addresses) >= limit:
                break

        return addresses

    except Exception as e:
        print("❌ Error scraping new pairs:", e)
        return []


def fetch_trending_pairs(limit: int = MAX_PAIRS_PER_CYCLE):
    """Fetch trending Solana pairs from Dexscreener's search endpoint."""
    try:
        resp = SESSION.get(TRENDING_SEARCH_URL, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            print("❌ Trending search error:", resp.status_code)
            return []

        data = resp.json()
        pairs = data.get("pairs") or []
        if not isinstance(pairs, list):
            return []

        return pairs[:limit]

    except Exception as e:
        print("❌ Error fetching trending pairs:", e)
        return []


# ----------------------------------
# FETCH FULL PAIR INFO FROM API
# ----------------------------------

def fetch_pairs_for_addresses(addresses: Iterable[str]):
    """
    For each pair address, call Dexscreener's pair API and collect pair objects.
    """
    pairs = []
    for addr in addresses:
        url = DEX_PAIR_API.format(addr)
        try:
            resp = SESSION.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                print(f"❌ API {resp.status_code} for pair {addr}")
                continue
            data = resp.json()
            api_pairs = data.get("pairs") or []
            if not isinstance(api_pairs, list) or not api_pairs:
                continue
            pairs.extend(api_pairs)
        except Exception as e:
            print(f"❌ Error fetching {addr}:", e)
            continue
        time.sleep(0.1)  # tiny delay to be polite with rate limits
    return pairs


# ----------------------------------
# FILTERS (NEW — BALANCED, ANTI-HONEYPOT, HIGH SIGNAL FLOW)
# ----------------------------------

def is_good_dex(pair) -> bool:
    """
    Allow only proper AMM pools on a short whitelist of DEXes.
    This helps avoid bonding curves / pump.fun / weird routing
    that your snipe route can't actually exit from.
    """
    dex_id   = (pair.get("dexId") or "").lower()
    dex_type = (pair.get("dexType") or "").lower()

    # Must be on a whitelisted DEX
    if dex_id not in ALLOWED_DEXES:
        return False

    # Must be an AMM-style pool, not bonding curve / limit orderbook etc.
    # Dexscreener usually uses "amm" for Raydium/Orca/Meteora pools.
    if dex_type and dex_type != "amm":
        return False

    return True

def is_valid_signal(pair):
    """
    Tighter, AMM-only, anti-honeypot filter.

      - Only AMM pools on ALLOWED_DEXES
      - Liquidity MIN_LIQ_USD–MAX_LIQ_USD
      - Age MIN_AGE_SEC–MAX_AGE_SEC
      - 5m buys >= 8, sells >= 2
      - buys >= 1.5x sells but ratio <= 12:1
      - 24h sells & volume high enough
      - reject high buy/sell tax if Dexscreener reports it
    """

    # --- DEX / pool sanity ---
    if not is_good_dex(pair):
        return False

    # --- Liquidity filter ---
    liq = safe_get(pair, "liquidity.usd", 0) or 0
    if liq < MIN_LIQ_USD or liq > MAX_LIQ_USD:
        return False

    # --- Age filter ---
    created_ms = safe_get(pair, "pairCreatedAt", None)
    if created_ms is None:
        return False
    age_sec = int((int(time.time() * 1000) - created_ms) / 1000)
    if age_sec < MIN_AGE_SEC or age_sec > MAX_AGE_SEC:
        return False

    # --- Flow filters ---
    buys_5m   = safe_get(pair, "txns.m5.buys", 0) or 0
    sells_5m  = safe_get(pair, "txns.m5.sells", 0) or 0
    buys_24h  = safe_get(pair, "txns.h24.buys", 0) or 0
    sells_24h = safe_get(pair, "txns.h24.sells", 0) or 0
    vol24     = safe_get(pair, "volume.h24", 0) or 0

    # Require real buy flow
    if buys_5m < 8:
        return False

    # Must be sellable (at least *some* sells in last 5m)
    if sells_5m < 2:
        return False

    # Buy momentum but not absurd
    if buys_5m < 1.5 * sells_5m:
        return False
    if sells_5m > 0 and buys_5m / sells_5m > 12:
        return False

    # Need a proper history of exits + volume
    if sells_24h < MIN_24H_SELLS:
        return False
    if vol24 < MIN_24H_VOL:
        return False

    # --- Tax filter (if Dexscreener exposes it) ---
    sell_tax = safe_get(pair, "info.sellTax", 0) or 0
    buy_tax  = safe_get(pair, "info.buyTax", 0) or 0
    # 15% is already pretty brutal for scalping; also catches many rugs
    if sell_tax >= 15 or buy_tax >= 15:
        return False

    return True

def compute_score(pair):
    """
    Compute a 0–100 score based on:
      - Liquidity
      - Age
      - Short-term buy pressure
      - Buy/sell health
    """
    score = 0

    # --- Liquidity ---
    liq = safe_get(pair, "liquidity.usd", 0) or 0
    if 8000 <= liq <= 20000:
        score += 30
    elif 5000 <= liq <= 25000:
        score += 15

    # --- Age ---
    age_sec = compute_pair_age_sec(pair)

    if age_sec >= 0:
        if 20 <= age_sec <= 150:
            score += 25
        elif 10 <= age_sec <= 240:
            score += 15

    # --- Flow ---
    buys_5m  = safe_get(pair, "txns.m5.buys", 0) or 0
    sells_5m = safe_get(pair, "txns.m5.sells", 0) or 0
    total_5m = buys_5m + sells_5m

    # Buy strength
    if buys_5m >= 20:
        score += 20
    elif buys_5m >= 10:
        score += 10

    # Buy/sell ratio quality
    if total_5m > 0:
        ratio = buys_5m / total_5m
        if ratio >= 0.75:
            score += 25
        elif ratio >= 0.6:
            score += 15

    # Clamp
    score = max(0, min(100, score))
    return score, age_sec, liq, buys_5m, sells_5m


def compute_tags(pair, age_sec, liq, buys_5m, sells_5m):
    """Generate smart-money style tags based on behaviour patterns."""
    tags = []

    vol24    = safe_get(pair, "volume.h24", 0) or 0
    buys_24h = safe_get(pair, "txns.h24.buys", 0) or 0
    sells_24h = safe_get(pair, "txns.h24.sells", 0) or 0
    txns_24h = buys_24h + sells_24h

    # Average trade size
    avg_trade = vol24 / txns_24h if txns_24h > 0 else 0

    if avg_trade >= 1000:
        tags.append("🐋 big average ticket size")
    elif avg_trade >= 300:
        tags.append("💰 mid-sized buyers active")

    # Early strong buy flow
    if (
        age_sec is not None
        and age_sec <= 180
        and buys_5m >= 25
        and buys_5m >= 2 * max(1, sells_5m)
    ):
        tags.append("⚡ strong early buy flow")

    # Profit taking
    if sells_5m >= 15 and sells_5m >= 0.6 * buys_5m:
        tags.append("⚠️ heavy profit-taking last 5m")

    # General healthy activity
    if txns_24h >= 200 and not tags:
        tags.append("📈 solid overall activity")

    return tags, buys_24h, sells_24h, vol24, avg_trade


# ----------------------------------
# PAPER TRADE HELPERS
# ----------------------------------

def maybe_open_paper_trade(pair, score, age_sec, tags_str):
    """Open a virtual snipe trade if we don't already track this pair."""
    pair_addr = pair.get("pairAddress", "unknown")
    if pair_addr in open_trades:
        return  # already in

    price = float(pair.get("priceUsd") or 0)
    if price <= 0:
        return

    base = pair.get("baseToken", {}) or {}
    symbol = base.get("symbol", "UNKNOWN")
    name = base.get("name", "")

    # 👇 NEW: snapshot of sells when we enter the trade
    sells_24h_entry = safe_get(pair, "txns.h24.sells", 0) or 0

    now = time.time()

    open_trades[pair_addr] = {
        "opened_at":        now,
        "symbol":           symbol,
        "name":             name,
        "entry_price":      price,
        "size_usd":         PAPER_TRADE_SIZE_USD,
        "score":            score,
        "tags":             tags_str,
        "sells_24h_entry":  sells_24h_entry,
    }

    print(f"{CYAN}📈 OPEN PAPER TRADE | {symbol} at ${price:.6f}{RESET}")


def detect_honeypot(pair, trade, age, sells_5m, buys_5m, sells_24h_now):
    """Evaluate honeypot-style red flags for an open trade."""

    honeypot_reasons = []

    # Dexscreener occasionally provides explicit flags
    if safe_get(pair, "info.isHoneypot", False) or safe_get(pair, "info.honeypot", False):
        honeypot_reasons.append("dexscreener_flag")

    sell_tax = safe_get(pair, "info.sellTax", 0) or 0
    buy_tax  = safe_get(pair, "info.buyTax", 0) or 0

    if sell_tax >= MAX_TAX_PCT or buy_tax >= MAX_TAX_PCT:
        honeypot_reasons.append("high_tax")

    ratio_5m = buys_5m / max(1, sells_5m)
    sells_24h_prev = trade.get("sells_24h_entry", 0)
    delta_sells_24h = sells_24h_now - sells_24h_prev

    if age > NO_SELLS_AFTER_SECONDS and sells_5m == 0 and buys_5m >= NO_SELLS_MIN_BUYS:
        honeypot_reasons.append("no_sells_recent")

    if buys_5m >= EXTREME_BUY_PRESSURE_BUYS and ratio_5m >= EXTREME_BUY_PRESSURE_RATIO:
        honeypot_reasons.append("insane_5m_buy_pressure")

    if age > MIN_SELL_DELTA_WINDOW and delta_sells_24h <= MAX_SELL_DELTA:
        honeypot_reasons.append("no_new_sells_since_entry")

    if age > STILL_LOW_SELLS_WINDOW and sells_24h_now <= sells_24h_prev + STILL_LOW_SELLS_ALLOWANCE:
        honeypot_reasons.append("still_no_liquidity_exit")

    return bool(honeypot_reasons), honeypot_reasons

def check_paper_trade_exit(pair):
    """Check if any open paper trade for this pair should be closed."""
    pair_addr = pair.get("pairAddress", "unknown")
    if pair_addr not in open_trades:
        return

    trade = open_trades[pair_addr]
    current_price = float(pair.get("priceUsd") or 0)
    if current_price <= 0:
        return

    entry = trade["entry_price"]
    now   = time.time()
    age   = now - trade["opened_at"]

    # --- Base PnL calculations ---
    multiple = current_price / entry
    pnl_pct  = (multiple - 1.0) * 100.0
    pnl_usd  = trade["size_usd"] * (multiple - 1.0)

    # --- Base exit reasons (before honeypot overrides) ---
    reason = None
    if multiple >= TP_MULTIPLIER:
        reason = "TP"
    elif multiple <= SL_MULTIPLIER:
        reason = "SL"
    elif age >= MAX_HOLD_SECONDS:
        reason = "TIME"

    # --- Fresh flow data for honeypot checks ---
    buys_5m        = safe_get(pair, "txns.m5.buys", 0) or 0
    sells_5m       = safe_get(pair, "txns.m5.sells", 0) or 0
    sells_24h_now  = safe_get(pair, "txns.h24.sells", 0) or 0
    honeypot_flag, honeypot_reasons = detect_honeypot(
        pair,
        trade,
        age,
        sells_5m=sells_5m,
        buys_5m=buys_5m,
        sells_24h_now=sells_24h_now,
    )

    # --- If it smells like a honeypot, do NOT allow a TP win ---
    if honeypot_flag and reason == "TP":
        reason   = "HONEYPOT"
        pnl_usd  = -trade["size_usd"]
        pnl_pct  = -100.0
        multiple = 0.0
        current_price = 0.0

    # Optional: force close obviously honeypotty trades even without TP/SL/TIME
    if honeypot_flag and reason is None and age > HONEYPOT_FORCE_EXIT_AFTER:
        reason   = "HONEYPOT"
        pnl_usd  = -trade["size_usd"]
        pnl_pct  = -100.0
        multiple = 0.0
        current_price = 0.0

    # If still no reason to close, keep trade open
    if reason is None:
        return

    # --- Log + print result ---
    if honeypot_flag:
        extra = f" (honeypot_reasons={','.join(honeypot_reasons)})"
    else:
        extra = ""

    print(
        f"{MAGENTA}💰 CLOSE PAPER TRADE [{reason}] | "
        f"{trade['symbol']} {pnl_pct:+.1f}% (${pnl_usd:+.2f}){extra}{RESET}"
    )

    log_paper_trade([
        datetime.utcfromtimestamp(trade["opened_at"]).isoformat(),
        datetime.utcfromtimestamp(now).isoformat(),
        pair_addr,
        trade["symbol"],
        trade["name"],
        entry,
        current_price,
        int(age),
        round(pnl_usd, 2),
        round(pnl_pct, 2),
        reason,
        trade["score"],
        trade["tags"],
    ])

    del open_trades[pair_addr]

def is_probably_tradable_on_axiom(pair) -> bool:
    """
    Heuristic check: is this pair likely tradable *for us* right now?
    Uses:
      - DEX whitelist
      - AMM-only
      - Liquidity + recent trades
    """
    if not is_good_dex(pair):
        return False

    liq_usd  = safe_get(pair, "liquidity.usd", 0) or 0
    buys_5m  = safe_get(pair, "txns.m5.buys", 0) or 0
    sells_5m = safe_get(pair, "txns.m5.sells", 0) or 0

    # Need a real LP, not a dust pool
    if liq_usd < MIN_LIQ_USD:
        return False

    # No trades in last 5m → pool might be dead / not open
    if (buys_5m + sells_5m) == 0:
        return False

    return True

def build_axiom_url(pair) -> str:
    """
    Build a best-guess Axiom trading URL from the token mint.
    Pattern:
        https://axiom.trade/meme/<mint>?chain=sol
    """
    base = pair.get("baseToken", {}) or {}
    mint = base.get("address", "")
    if not mint:
        return ""
    return f"https://axiom.trade/meme/{mint}?chain=sol"


# ----------------------------------
# CSV LOGGING
# ----------------------------------

def log_signal(row):
    first_write = False
    try:
        with open(LOG_FILE, "r"):
            pass
    except FileNotFoundError:
        first_write = True

    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if first_write:
            writer.writerow([
                "timestamp", "pairAddress", "symbol", "name",
                "age_sec", "liquidity_usd", "buys_5m", "sells_5m",
                "vol24h", "score", "tags"
            ])
        writer.writerow(row)


def log_paper_trade(row):
    """Append a completed paper trade to CSV."""
    first_write = False
    try:
        with open(TRADES_LOG_FILE, "r"):
            pass
    except FileNotFoundError:
        first_write = True

    with open(TRADES_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if first_write:
            writer.writerow([
                "timestamp_open",
                "timestamp_close",
                "pairAddress",
                "symbol",
                "name",
                "entry_price_usd",
                "exit_price_usd",
                "hold_seconds",
                "pnl_usd",
                "pnl_pct",
                "reason",    # "TP", "SL", "TIME", "HONEYPOT"
                "score",
                "tags",
            ])
        writer.writerow(row)

def run_bot() -> None:
    """Main loop to poll Dexscreener, surface signals, and paper-trade them."""
    mode_label = "Cupsey-style FLOW mode (trending)" if FLOW_MODE else "New-pairs sniper mode"
    source_url = TRENDING_SEARCH_URL if FLOW_MODE else NEW_PAIRS_URL

    print(f"🚀 Starting AI-Style Memecoin Scanner ({mode_label})…")
    print(f"Source: {source_url}\n")

    seen_pairs = set()  # to avoid alerting the same pair again and again

    while True:
        # 1) Update all existing paper trades first
        if open_trades:
            try:
                open_addrs = list(open_trades.keys())
                open_pairs = fetch_pairs_for_addresses(open_addrs)
                for op in open_pairs:
                    check_paper_trade_exit(op)
            except Exception as e:
                print("⚠️ Error updating open trades:", e)

        # 2) Choose discovery path based on mode
        if FLOW_MODE:
            raw_pairs = fetch_trending_pairs()
            if not raw_pairs:
                print("⚠️ No trending pairs fetched this cycle.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            pairs = []
            for p in raw_pairs:
                addr = p.get("pairAddress")
                if not addr or addr in seen_pairs:
                    continue
                seen_pairs.add(addr)
                pairs.append(p)

            if not pairs:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue
        else:
            addrs = fetch_new_pair_addresses()
            if not addrs:
                print("⚠️ No addresses scraped this cycle.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Skip already-seen pairs so we only alert once per pair
            new_addrs = [a for a in addrs if a not in seen_pairs]
            for a in new_addrs:
                seen_pairs.add(a)

            if not new_addrs:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Fetch full pair data for those new addresses
            pairs = fetch_pairs_for_addresses(new_addrs)

        # 3) Process each pair
        for p in pairs:
            try:
                # Skip obviously untradable stuff first
                if not is_probably_tradable_on_axiom(p):
                    continue

                # Structural + behavioural filters
                if FLOW_MODE:
                    if not FLOW.is_valid_flow(p):
                        continue
                    score = FLOW.compute_flow_score(p)
                    age_sec = compute_pair_age_sec(p)
                    liq = safe_get(p, "liquidity.usd", 0) or 0
                    buys_5m = safe_get(p, "txns.m5.buys", 0) or 0
                    sells_5m = safe_get(p, "txns.m5.sells", 0) or 0
                else:
                    if not is_valid_signal(p):
                        continue
                    score, age_sec, liq, buys_5m, sells_5m = compute_score(p)

                # Allow only good setups (tune 55/60 as you like)
                min_score = 60 if FLOW_MODE else 55
                if score < min_score:
                    continue

                # Smart-money style tags
                tags, buys_24h, sells_24h, vol24, avg_trade = compute_tags(
                    p, age_sec, liq, buys_5m, sells_5m
                )
                tags_str = ", ".join(tags) if tags else ""

                base      = p.get("baseToken", {}) or {}
                symbol    = base.get("symbol", "UNKNOWN")
                name      = base.get("name", "")
                pair_addr = p.get("pairAddress", "unknown")
                axiom_url = build_axiom_url(p)
                col       = colour_for_score(score)
                label     = "FLOW SIGNAL" if FLOW_MODE else "SIGNAL"

                # Pretty print
                print(f"{col}🔥 {label} (score {score}/100) | {symbol} ({name}){RESET}")
                print(f"{col}   Pair: {pair_addr}{RESET}")
                print(f"{col}   Age: {age_sec}s{RESET}")
                print(f"{col}   Liquidity: ${liq:,.0f}{RESET}")
                print(f"{col}   5m Buys: {buys_5m} vs {sells_5m} sells{RESET}")
                print(f"{col}   24h Volume: ${vol24:,.0f}{RESET}")
                print(f"{col}   Tags: {tags_str if tags_str else '(none)'}{RESET}")
                if axiom_url:
                    print(f"{col}   Axiom: {axiom_url}{RESET}\n")
                else:
                    print()

                # Log signal
                log_signal([
                    datetime.utcnow().isoformat(),
                    pair_addr,
                    symbol,
                    name,
                    age_sec,
                    liq,
                    buys_5m,
                    sells_5m,
                    vol24,
                    score,
                    tags_str,
                ])

                # Open a new paper trade on this signal
                maybe_open_paper_trade(p, score, age_sec, tags_str)

            except Exception as e:
                print("⚠️ Error handling pair:", e)

        # 5) Wait until next cycle
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n👋 Shutting down scanner.")
