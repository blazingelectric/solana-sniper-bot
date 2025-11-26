import requests
import csv
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup

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

CHECK_INTERVAL_SECONDS = 10      # how often to poll the new-pairs page
MAX_PAIRS_PER_CYCLE    = 20      # don’t hammer the API too hard
LOG_FILE               = "meme_signals.csv"

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

def fetch_new_pair_addresses(limit=MAX_PAIRS_PER_CYCLE):
    """
    Scrape Dexscreener new Solana pairs page and extract newest pair addresses.
    We look for <a href="/solana/<pairAddress>"> links.
    """
    try:
        resp = requests.get(
            NEW_PAIRS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
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


# ----------------------------------
# FETCH FULL PAIR INFO FROM API
# ----------------------------------

def fetch_pairs_for_addresses(addresses):
    """
    For each pair address, call Dexscreener's pair API and collect pair objects.
    """
    pairs = []
    for addr in addresses:
        url = DEX_PAIR_API.format(addr)
        try:
            resp = requests.get(url, timeout=10)
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
    created_ms = safe_get(pair, "pairCreatedAt", None)
    age_sec = -1
    if created_ms is not None:
        now_ms = int(time.time() * 1000)
        age_sec = int((now_ms - created_ms) / 1000)

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
    sells_24h_prev = trade.get("sells_24h_entry", 0)
    delta_sells_24h = sells_24h_now - sells_24h_prev

    honeypot_flag = False

    # Tax data (NEW)
    sell_tax = safe_get(pair, "info.sellTax", 0) or 0
    buy_tax  = safe_get(pair, "info.buyTax", 0) or 0

    if sell_tax >= 15 or buy_tax >= 15:
        honeypot_flag = True

    honeypot_reasons = []

    ratio_5m = buys_5m / max(1, sells_5m)

    # A1) 60+ seconds old, basically no sells recently
    if age > 60 and sells_5m == 0 and buys_5m >= 10:
        honeypot_flag = True
        honeypot_reasons.append("no_sells_5m_after_60s")

    # A2) Very skewed buy/sell ratio in 5m window
    if buys_5m >= 25 and ratio_5m >= 15:
        honeypot_flag = True
        honeypot_reasons.append("insane_5m_buy_pressure")

    # A3) Over lifetime: almost no new sells since we entered
    if age > 5 * 60 and delta_sells_24h <= 2:
        honeypot_flag = True
        honeypot_reasons.append("no_new_sells_since_entry")

    # A4) 10+ min in trade, still very few sells overall
    if age > 10 * 60 and sells_24h_now <= sells_24h_prev + 3:
        honeypot_flag = True
        honeypot_reasons.append("still_no_liquidity_exit")

    # --- If it smells like a honeypot, do NOT allow a TP win ---
    if honeypot_flag and reason == "TP":
        reason   = "HONEYPOT"
        pnl_usd  = -trade["size_usd"]
        pnl_pct  = -100.0
        multiple = 0.0
        current_price = 0.0

    # Optional: force close obviously honeypotty trades even without TP/SL/TIME
    if honeypot_flag and reason is None and age > 8 * 60:
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

# ----------------------------------
# MAIN LOOP
# ----------------------------------

print("🚀 Starting AI-Style Memecoin Scanner (Dexscreener new pairs)…")
print(f"Source: {NEW_PAIRS_URL}\n")

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

    # 2) Fetch newest pairs from Dexscreener
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

    # 3) Fetch full pair data for those new addresses
    pairs = fetch_pairs_for_addresses(new_addrs)

    # 4) Process each pair
    for p in pairs:
        try:
            # Skip obviously untradable stuff first
            if not is_probably_tradable_on_axiom(p):
                continue

            # Structural + behavioural filters
            if not is_valid_signal(p):
                continue

            # Compute score
            score, age_sec, liq, buys_5m, sells_5m = compute_score(p)

            # Allow only good setups (tune 55/60 as you like)
            if score < 55:
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

            # Pretty print
            print(f"{col}🔥 SIGNAL (score {score}/100) | {symbol} ({name}){RESET}")
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
