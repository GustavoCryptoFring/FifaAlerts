#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
World Cup 2026 Polymarket -> Telegram alert bot (single file).

БЫСТРЫЙ СТАРТ (на VPS):
  1) python3 -m venv venv && source venv/bin/activate
  2) pip install requests
  3) Задай переменные окружения (или впиши в CONFIG ниже):
       export TG_BOT_TOKEN="123456:ABC..."     # токен от @BotFather
       export TG_CHAT_ID="-1001234567890"       # id чата/канала, где бот админ
  4) python3 worldcup_polymarket_bot.py
  (для автозапуска удобно завернуть в systemd-сервис, см. README в конце файла)

Что делает бот (только по матчам World Cup со стартом СЕГОДНЯ по UTC):
  1) Резкое изменение шанса на победу (moneyline): >= MOVE_THRESHOLD за окно MOVE_WINDOW_SEC.
  2) Smart money: суммарная покупка/продажа одного кошелька по одному исходу
     за SMART_MONEY_WINDOW_SEC >= SMART_MONEY_USD.
  3) Топ-10 держателей по каждому исходу: сообщает кто докупил/сбросил.
  4) Кит: любая позиция (shares * цена) >= WHALE_USD.

Все тексты уведомлений — на английском. К каждой ссылке Polymarket добавляется ?via=...
Бот никогда не торгует и не требует приватного ключа — только публичные read-only API.
"""

import os
import re
import sys
import json
import time
import html
import logging
from collections import deque, defaultdict
from datetime import datetime, timezone, date, timedelta

import requests

# =========================== CONFIG ===========================
# Значения можно задать через переменные окружения (рекомендуется для GitHub,
# чтобы не светить токен), либо впрямую здесь.

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")          # @BotFather token
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")            # chat / channel id (bot must be admin)

REF_CODE = os.getenv("PM_REF", "WorldCupPolymarket2026")  # ?via=<REF_CODE> on every link

# --- Thresholds (пороги) ---
MOVE_THRESHOLD       = float(os.getenv("MOVE_THRESHOLD", "0.03"))   # 0.03 = 3 процентных пункта (55->52)
MOVE_WINDOW_SEC      = int(os.getenv("MOVE_WINDOW_SEC", "900"))     # 15 минут
MOVE_COOLDOWN_SEC    = int(os.getenv("MOVE_COOLDOWN_SEC", "300"))   # не повторять тот же алерт чаще, чем раз в 5 мин

SMART_MONEY_USD        = float(os.getenv("SMART_MONEY_USD", "10000"))   # $10k (любой кошелёк, п.2)
SMART_MONEY_WINDOW_SEC = int(os.getenv("SMART_MONEY_WINDOW_SEC", "300"))# суммировать за 5 минут

WHALE_USD = float(os.getenv("WHALE_USD", "100000"))   # $100k в одной позиции (п.4)

TOP_HOLDERS_N = int(os.getenv("TOP_HOLDERS_N", "10"))  # топ-N держателей на исход (watchlist)

# Пункт 5: сделки аккаунтов из топ-10 (watchlist) по ML/spread/total
WATCH_TRADE_USD        = float(os.getenv("WATCH_TRADE_USD", "5000"))    # $5k
WATCH_TRADE_WINDOW_SEC = int(os.getenv("WATCH_TRADE_WINDOW_SEC", "600"))# за 10 минут

# --- Poll intervals (интервалы опроса) ---
TICK_SEC         = int(os.getenv("TICK_SEC", "60"))         # общий тик бота — 1 минута
HOLDERS_POLL_SEC = int(os.getenv("HOLDERS_POLL_SEC", "180"))# обновление watchlist держателей/китов
GAMES_REFRESH_SEC= int(os.getenv("GAMES_REFRESH_SEC", "600"))# обновление списка игр

# --- Game discovery (поиск игр) ---
# Если автопоиск тега не сработает на твоём VPS — задай WORLD_CUP_TAG_ID
# или явный список слагов событий через GAMES_SLUGS="fifwc-che-can-2026-06-24,fifwc-bih-qat-2026-06-24"
WORLD_CUP_TAG_ID = os.getenv("WORLD_CUP_TAG_ID", "")
GAMES_SLUGS      = [s.strip() for s in os.getenv("GAMES_SLUGS", "").split(",") if s.strip()]
EVENT_SLUG_PREFIX= os.getenv("EVENT_SLUG_PREFIX", "fifwc")  # слаг игр World Cup начинается с этого

# Сколько дней вперёд от сегодняшней даты (UTC) отслеживать.
# 0 = только сегодня (как в исходном задании). 3 = сегодня + 3 ближайших дня.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "0"))

# "Чистый" слаг основного матча: fifwc-<team>-<team>-YYYY-MM-DD (без суффиксов вроде -total-corners).
GAME_SLUG_RE = re.compile(r"^" + re.escape(EVENT_SLUG_PREFIX) + r"-[a-z0-9]+-[a-z0-9]+-(\d{4})-(\d{2})-(\d{2})$")

# --- API hosts ---
GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()
# ==============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("wc-bot")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "wc-polymarket-bot/1.0", "Accept": "application/json"})


# =========================== HTTP helpers ===========================
def http_get(url, params=None, tries=3):
    """GET JSON with small backoff. Returns parsed JSON or None (never raises)."""
    for attempt in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                wait = 2 * (attempt + 1)
                log.warning("429 rate limited on %s, sleeping %ss", url, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == tries - 1:
                log.warning("GET failed %s params=%s : %s", url, params, e)
            else:
                time.sleep(1.0 * (attempt + 1))
    return None


def parse_json_array(value):
    """Gamma returns outcomes/outcomePrices/clobTokenIds as stringified JSON arrays."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def pm_game_url(event_slug):
    """Build the Polymarket game URL with the referral query param."""
    base = f"https://polymarket.com/sports/world-cup/{event_slug}"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}via={REF_CODE}"


def pm_profile_url(wallet):
    """Build a Polymarket profile URL (by proxy wallet) with the referral param."""
    return f"https://polymarket.com/profile/{wallet}?via={REF_CODE}"


# =========================== Telegram ===========================
def tg_send(text):
    """Send an HTML message to the configured chat. Best effort."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.error("Telegram not configured (TG_BOT_TOKEN / TG_CHAT_ID). Message dropped:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            r = SESSION.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                retry = r.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(retry + 1)
                continue
            r.raise_for_status()
            return
        except Exception as e:
            log.warning("Telegram send failed (attempt %s): %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))


def esc(s):
    return html.escape(str(s))


# =========================== Polymarket API ===========================
PROP_KEYWORDS = (
    "corner", "card", "booking", "yellow", "red card", "foul", "offside",
    "shot", "save", "possession", "goalscorer", "scorer", "assist",
    "clean sheet", "penalty", "header", "throw-in", "free kick", "substitut",
)


def classify_market(m):
    """Return 'moneyline' | 'spread' | 'total' | 'other' using several heuristics.
    Props (corners, cards, etc.) are explicitly excluded -> 'other'."""
    text = (str(m.get("slug", "")) + " " + str(m.get("question", "")) + " " +
            str(m.get("groupItemTitle", ""))).lower()
    # Never treat a prop market as moneyline/spread/total
    if any(k in text for k in PROP_KEYWORDS):
        return "other"
    for key in ("sportsMarketType", "marketType", "type"):
        v = str(m.get(key, "")).lower()
        if v in ("moneyline", "spreads", "spread", "totals", "total"):
            return {"spreads": "spread", "totals": "total"}.get(v, v)
    if "moneyline" in text:
        return "moneyline"
    if "spread" in text:
        return "spread"
    if "total" in text or "over/under" in text or "o/u" in text:
        return "total"
    # Outcome-based detection (e.g. "Over 2.5"/"Under 2.5", "CHE -1.5"/"CAN +1.5")
    outs = [str(o).lower() for o in parse_json_array(m.get("outcomes"))]
    joined = " ".join(outs)
    if any(o.startswith("over") or o.startswith("under") for o in outs):
        return "total"
    if re.search(r"[+\-]\d", joined):
        return "spread"
    if any("draw" in o for o in outs):
        return "moneyline"
    return "other"


def market_volume(m):
    for key in ("volume", "volumeNum", "volume24hr", "volumeClob"):
        try:
            v = float(m.get(key))
            if v:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def build_token_meta(event):
    """
    From one game event build:
      - game label and slug
      - per-token metadata (outcome name, market type, conditionId)
      - moneyline token ids (for price-move detection)
      - condition_ids: all markets (for the smart-money scan, requirement 2)
      - holder_market_conds: moneyline + the single highest-volume spread + total line
        (these 7-ish outcomes are what we track for top-holder watchlist & whales)
    """
    slug = event.get("slug", "")
    label = event.get("title") or slug
    token_meta = {}
    moneyline_tokens = []
    condition_ids = []
    by_type = {"moneyline": [], "spread": [], "total": []}

    for m in event.get("markets", []) or []:
        if m.get("closed") is True:
            continue
        cond = m.get("conditionId")
        if not cond:
            continue
        mtype = classify_market(m)
        outcomes = parse_json_array(m.get("outcomes"))
        tokens = parse_json_array(m.get("clobTokenIds"))
        if not tokens:
            continue
        condition_ids.append(cond)
        for i, tok in enumerate(tokens):
            tok = str(tok)
            outcome = outcomes[i] if i < len(outcomes) else f"outcome{i}"
            token_meta[tok] = {
                "outcome": outcome,
                "mtype": mtype,
                "conditionId": cond,
                "slug": slug,
                "label": label,
            }
            if mtype == "moneyline":
                moneyline_tokens.append(tok)
        if mtype in by_type:
            by_type[mtype].append({"cond": cond, "vol": market_volume(m)})

    # holder markets: all moneyline + the single most-traded spread + total line
    holder_market_conds = [x["cond"] for x in by_type["moneyline"]]
    for t in ("spread", "total"):
        if by_type[t]:
            best = max(by_type[t], key=lambda x: x["vol"])
            holder_market_conds.append(best["cond"])

    return {
        "slug": slug,
        "label": label,
        "token_meta": token_meta,
        "moneyline_tokens": moneyline_tokens,
        "condition_ids": list(dict.fromkeys(condition_ids)),
        "holder_market_conds": list(dict.fromkeys(holder_market_conds)),
    }


_TAG_CACHE = "UNSET"  # cache the resolved World Cup tag id across refreshes


def iter_tags(max_pages=30, page=1000):
    """Yield tag dicts across all /tags pages (offset pagination)."""
    offset = 0
    for _ in range(max_pages):
        batch = http_get(f"{GAMMA}/tags", params={"limit": page, "offset": offset})
        if not isinstance(batch, list) or not batch:
            return
        for t in batch:
            if isinstance(t, dict):
                yield t
        if len(batch) < page:
            return
        offset += page


def looks_world_cup(t):
    slug = str(t.get("slug", "")).lower()
    label = str(t.get("label", t.get("name", ""))).lower()
    if slug.startswith(EVENT_SLUG_PREFIX):
        return True
    if "world-cup" in slug:
        return True
    if "world" in label and "cup" in label:
        return True
    if "fifa" in slug or "fifa" in label:
        return True
    return False


def game_date_from_slug(slug):
    """Extract the match date (UTC) embedded in a main-game slug, or None."""
    m = GAME_SLUG_RE.match(str(slug))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def is_main_game_event(ev):
    """True only for real match events (clean slug, not a -total-corners/-props sub-event)."""
    return isinstance(ev, dict) and GAME_SLUG_RE.match(str(ev.get("slug", ""))) is not None


def fetch_tag_events(tag_id, limit=400):
    out, offset = [], 0
    while offset <= limit:
        batch = http_get(f"{GAMMA}/events", params={
            "tag_id": tag_id, "closed": "false", "active": "true",
            "limit": 100, "offset": offset})
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return out


def count_main_games(tag_id):
    """How many real match events live under this tag (used to pick the best tag)."""
    evs = fetch_tag_events(tag_id)
    mains = [e for e in evs if is_main_game_event(e)]
    return len(mains), mains


def candidate_tag_ids_from_sports():
    """Read /sports (each entry has 'sport' slug + comma-separated 'tags' ids) and
    return tag ids belonging to World Cup / soccer entries."""
    out, seen = [], set()
    sports = http_get(f"{GAMMA}/sports")
    if not isinstance(sports, list):
        return out
    for sp in sports:
        if not isinstance(sp, dict):
            continue
        slug = str(sp.get("sport", "")).lower()
        if not any(k in slug for k in ("world", "fifa", "fifwc", "soccer", "football")):
            continue
        tags = sp.get("tags")
        if isinstance(tags, str):
            parts = [x.strip() for x in tags.split(",") if x.strip()]
        elif isinstance(tags, list):
            parts = [str(x).strip() for x in tags]
        else:
            parts = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def discover_tag_id():
    """Pick the tag with the MOST real World Cup match events. Cached after success."""
    global _TAG_CACHE
    if WORLD_CUP_TAG_ID:
        return WORLD_CUP_TAG_ID
    if _TAG_CACHE != "UNSET":
        return _TAG_CACHE
    best, best_n = None, 0
    try:
        candidates = list(candidate_tag_ids_from_sports())
        for tid in candidates:
            n, _ = count_main_games(tid)
            log.info("tag %s -> %d main game(s)", tid, n)
            if n > best_n:
                best, best_n = tid, n
        # Fallback: if /sports tags held no real matches, scan all /tags
        if best_n == 0:
            log.info("No main games under /sports tags; scanning /tags as fallback")
            for t in iter_tags():
                if not looks_world_cup(t):
                    continue
                tid = str(t.get("id"))
                n, _ = count_main_games(tid)
                if n > best_n:
                    best, best_n = tid, n
                    if n >= 4:   # clearly the games tag; stop early
                        break
    except Exception as e:
        log.warning("tag discovery failed (%s)", e)
    if best:
        log.info("World Cup tag resolved: %s (%d main games)", best, best_n)
        _TAG_CACHE = best
    else:
        log.warning("No tag with real match events found. "
                    "Set WORLD_CUP_TAG_ID or GAMES_SLUGS to be safe.")
    return best


def fetch_events_by_tag(tag_id):
    out = []
    offset = 0
    while True:
        batch = http_get(f"{GAMMA}/events", params={
            "tag_id": tag_id, "closed": "false", "active": "true",
            "limit": 100, "offset": offset,
        })
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        if offset > 1000:
            break
    return out


def fetch_events_by_slug_scan():
    """Fallback: page through open events and keep World Cup game slugs."""
    out = []
    offset = 0
    while offset <= 1500:  # a few pages, bounded
        batch = http_get(f"{GAMMA}/events", params={
            "closed": "false", "active": "true", "limit": 100, "offset": offset,
        })
        if not isinstance(batch, list) or not batch:
            break
        for ev in batch:
            if str(ev.get("slug", "")).startswith(EVENT_SLUG_PREFIX):
                out.append(ev)
        if len(batch) < 100:
            break
        offset += 100
    return out


def fetch_events_by_explicit_slugs(slugs):
    out = []
    for s in slugs:
        res = http_get(f"{GAMMA}/events", params={"slug": s})
        if isinstance(res, list):
            out.extend(res)
    return out


def is_today_utc(event):
    """Keep events whose startDate is today's UTC date and which are not closed."""
    if event.get("closed") is True:
        return False
    sd = event.get("startDate") or event.get("gameStartTime")
    if not sd:
        return False
    try:
        # ISO 8601, may end with Z
        dt = datetime.fromisoformat(str(sd).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
    except Exception:
        return False
    return dt.date() == datetime.now(timezone.utc).date()


def in_date_window(slug):
    """True if the match date in the slug is within [today, today+DAYS_AHEAD] (UTC)."""
    gdate = game_date_from_slug(slug)
    if gdate is None:
        return False
    today = datetime.now(timezone.utc).date()
    return today <= gdate <= (today + timedelta(days=DAYS_AHEAD))


def get_today_games():
    """Return real World Cup match events within the date window."""
    if GAMES_SLUGS:
        events = fetch_events_by_explicit_slugs(GAMES_SLUGS)
    else:
        tag = discover_tag_id()
        events = fetch_events_by_tag(tag) if tag else []
        if not events:
            events = fetch_events_by_slug_scan()

    games = []
    seen = set()
    for ev in events:
        slug = ev.get("slug", "")
        if slug in seen:
            continue
        if not GAMES_SLUGS:
            if not is_main_game_event(ev):     # skip props like -total-corners
                continue
            if ev.get("closed") is True:
                continue
            if not in_date_window(slug):       # date taken from the slug itself
                continue
        seen.add(slug)
        g = build_token_meta(ev)
        if g["condition_ids"]:
            games.append(g)
    return games


def get_price(token_id, cache=None):
    """Current price (probability) for an outcome token via CLOB midpoint, fallback last price."""
    if cache is not None and token_id in cache:
        return cache[token_id]
    price = None
    mid = http_get(f"{CLOB}/midpoint", params={"token_id": token_id})
    if isinstance(mid, dict) and mid.get("mid") is not None:
        try:
            price = float(mid["mid"])
        except Exception:
            price = None
    if price is None:
        pr = http_get(f"{CLOB}/price", params={"token_id": token_id, "side": "buy"})
        if isinstance(pr, dict) and pr.get("price") is not None:
            try:
                price = float(pr["price"])
            except Exception:
                price = None
    if cache is not None and price is not None:
        cache[token_id] = price
    return price


def get_trades(condition_id, limit=100):
    res = http_get(f"{DATA}/trades", params={"market": condition_id, "limit": limit})
    return res if isinstance(res, list) else []


def get_holders(condition_id, limit=200):
    """Returns list of {token, holders:[{proxyWallet, amount, name,...}]}. Shape-tolerant."""
    res = http_get(f"{DATA}/holders", params={"market": condition_id, "limit": limit})
    norm = []
    if isinstance(res, list):
        for item in res:
            if not isinstance(item, dict):
                continue
            token = item.get("token") or item.get("asset") or item.get("tokenId")
            holders = item.get("holders") or item.get("holder") or []
            norm.append({"token": str(token) if token else None, "holders": holders})
    elif isinstance(res, dict) and "holders" in res:
        norm.append({"token": res.get("token"), "holders": res.get("holders", [])})
    return norm


def holder_name(h):
    name = h.get("name") or h.get("pseudonym")
    wallet = h.get("proxyWallet") or h.get("wallet") or ""
    if name:
        return str(name)
    if wallet:
        return wallet[:6] + "..." + wallet[-4:]
    return "unknown"


# =========================== State ===========================
class State:
    def __init__(self):
        # price-move
        self.price_hist = defaultdict(lambda: deque())   # token -> deque[(ts, price)]
        self.move_cooldown = {}                          # token -> ts_until
        # smart money
        self.trade_seen = deque(maxlen=20000)            # recent tx fingerprints
        self.trade_seen_set = set()
        self.trade_watermark = {}                        # conditionId -> last ts seen
        self.sm_buffer = deque()                         # (ts, wallet, token, side, usd, meta)
        self.sm_alerted = {}                             # (wallet, token, side) -> ts_until
        # holders / whales
        self.whales = set()                              # (token, wallet) currently >= WHALE_USD
        # watchlist (top-10 holders of ML/spread/total) + their trades (requirement 5)
        self.watchlist = set()                           # lowercased proxy wallets
        self.wt_watermark = {}                           # conditionId -> last ts seen
        self.wt_seen = deque(maxlen=20000)
        self.wt_seen_set = set()
        self.wt_buffer = deque()                         # (ts, wallet, token, side, usd, meta)
        self.wt_alerted = {}                             # (wallet, token, side) -> ts_until

STATE = State()


# =========================== Detectors ===========================
def detect_price_moves(games):
    now = time.time()
    price_cache = {}
    for g in games:
        for tok in g["moneyline_tokens"]:
            price = get_price(tok, price_cache)
            if price is None:
                continue
            hist = STATE.price_hist[tok]
            hist.append((now, price))
            # drop samples older than the window
            while hist and now - hist[0][0] > MOVE_WINDOW_SEC:
                hist.popleft()
            if len(hist) < 2:
                continue
            if STATE.move_cooldown.get(tok, 0) > now:
                continue
            prices = [p for _, p in hist]
            wmin, wmax = min(prices), max(prices)
            meta = g["token_meta"].get(tok, {})
            outcome = meta.get("outcome", "?")
            label = meta.get("label", g["label"])
            link = pm_game_url(g["slug"])
            direction = None
            ref = None
            if price - wmin >= MOVE_THRESHOLD:
                direction, ref = "📈 up", wmin
            elif wmax - price >= MOVE_THRESHOLD:
                direction, ref = "📉 down", wmax
            if direction:
                delta = (price - ref) * 100
                msg = (
                    f"{direction} <b>Odds move</b>\n"
                    f"<b>{esc(label)}</b> — {esc(outcome)}\n"
                    f"{ref*100:.1f}% → {price*100:.1f}% "
                    f"({delta:+.1f} pts in ≤{MOVE_WINDOW_SEC//60}m)\n"
                    f'<a href="{link}">Open game</a>'
                )
                tg_send(msg)
                STATE.move_cooldown[tok] = now + MOVE_COOLDOWN_SEC


def detect_smart_money(games):
    now = time.time()
    # collect new trades from every tracked market
    for g in games:
        for cond in g["condition_ids"]:
            trades = get_trades(cond, limit=100)
            wm = STATE.trade_watermark.get(cond, 0)
            newest = wm
            for t in trades:
                ts = int(t.get("timestamp", 0) or 0)
                if ts < wm - SMART_MONEY_WINDOW_SEC:  # well past window, stop scanning deeper
                    continue
                txh = t.get("transactionHash", "")
                wallet = t.get("proxyWallet", "")
                asset = str(t.get("asset", ""))
                size = float(t.get("size", 0) or 0)
                fp = f"{txh}|{wallet}|{asset}|{t.get('side')}|{size}"
                if fp in STATE.trade_seen_set:
                    continue
                STATE.trade_seen_set.add(fp)
                STATE.trade_seen.append(fp)
                if len(STATE.trade_seen) == STATE.trade_seen.maxlen:
                    old = STATE.trade_seen.popleft()
                    STATE.trade_seen_set.discard(old)
                if ts <= wm:
                    continue
                newest = max(newest, ts)
                usd = t.get("usdcSize")
                price = float(t.get("price", 0) or 0)
                usd = float(usd) if usd is not None else size * price
                side = str(t.get("side", "")).upper()
                STATE.sm_buffer.append((ts, wallet, asset, side, usd, {
                    "outcome": t.get("outcome", "?"),
                    "label": t.get("title", g["label"]),
                    "slug": g["slug"],
                    "wallet": wallet,
                    "name": t.get("name") or t.get("pseudonym") or (wallet[:6] + "..." + wallet[-4:] if wallet else "unknown"),
                }))
            STATE.trade_watermark[cond] = newest

    # purge old buffer entries
    while STATE.sm_buffer and now - STATE.sm_buffer[0][0] > SMART_MONEY_WINDOW_SEC:
        STATE.sm_buffer.popleft()
    # clear expired alert locks
    for k in [k for k, exp in STATE.sm_alerted.items() if exp < now]:
        STATE.sm_alerted.pop(k, None)

    # aggregate by (wallet, token, side) within window
    agg = defaultdict(lambda: [0.0, None])
    for ts, wallet, token, side, usd, meta in STATE.sm_buffer:
        key = (wallet, token, side)
        agg[key][0] += usd
        agg[key][1] = meta
    for (wallet, token, side), (total, meta) in agg.items():
        if total < SMART_MONEY_USD:
            continue
        if STATE.sm_alerted.get((wallet, token, side), 0) > now:
            continue
        STATE.sm_alerted[(wallet, token, side)] = now + SMART_MONEY_WINDOW_SEC
        verb = "is accumulating" if side == "BUY" else "is dumping"
        icon = "🟢" if side == "BUY" else "🔴"
        link = pm_game_url(meta["slug"])
        name_link = (f'<a href="{pm_profile_url(meta["wallet"])}">{esc(meta["name"])}</a>'
                     if meta.get("wallet") else f"<b>{esc(meta['name'])}</b>")
        msg = (
            f"{icon} <b>Smart money — {side}</b>\n"
            f"{name_link} {verb} <b>{esc(meta['outcome'])}</b>\n"
            f"{esc(meta['label'])}\n"
            f"≈ ${total:,.0f} in ≤{SMART_MONEY_WINDOW_SEC//60}m\n"
            f'<a href="{link}">Open game</a>'
        )
        tg_send(msg)


def refresh_holders_and_whales(games):
    """Rebuild the top-10 watchlist over ML/spread/total markets and emit whale alerts (req 4)."""
    now = time.time()
    price_cache = {}
    new_watch = set()
    for g in games:
        for cond in g["holder_market_conds"]:
            blocks = get_holders(cond, limit=200)
            for block in blocks:
                token = block.get("token")
                holders = block.get("holders") or []
                rows = []
                for h in holders:
                    amt = h.get("amount", h.get("shares"))
                    try:
                        amt = float(amt)
                    except Exception:
                        continue
                    wallet = (h.get("proxyWallet") or h.get("wallet") or "")
                    rows.append((wallet, amt, h))
                if not rows:
                    continue
                rows.sort(key=lambda x: x[1], reverse=True)

                # watchlist: top-10 holders of this outcome token
                for wallet, _amt, _h in rows[:TOP_HOLDERS_N]:
                    if wallet:
                        new_watch.add(wallet.lower())

                meta = g["token_meta"].get(str(token), {}) if token else {}
                outcome = meta.get("outcome", "?")
                label = meta.get("label", g["label"])
                link = pm_game_url(g["slug"])
                price = get_price(str(token), price_cache) if token else None

                # ---- (4) whales: any position >= WHALE_USD ----
                if price:
                    for wallet, amt, h in rows:
                        value = amt * price
                        key = (str(token), wallet)
                        if value >= WHALE_USD:
                            if key not in STATE.whales:
                                STATE.whales.add(key)
                                name_link = (
                                    f'<a href="{pm_profile_url(wallet)}">{esc(holder_name(h))}</a>'
                                    if wallet else f"<b>{esc(holder_name(h))}</b>")
                                msg = (
                                    f"🐋 <b>Whale position</b>\n"
                                    f"{name_link} holds "
                                    f"{amt:,.0f} shares of <b>{esc(outcome)}</b>\n"
                                    f"{esc(label)} @ {price*100:.1f}% ≈ <b>${value:,.0f}</b>\n"
                                    f"Strong conviction on this outcome.\n"
                                    f'<a href="{link}">Open game</a>'
                                )
                                tg_send(msg)
                        else:
                            STATE.whales.discard(key)

    STATE.watchlist = new_watch
    log.info("watchlist size: %d wallets", len(new_watch))


BET_TYPE_LABEL = {"moneyline": "Moneyline", "spread": "Spread", "total": "Total"}


def detect_watched_trades(games):
    """(req 5) Alert when a watchlist account trades >= WATCH_TRADE_USD on one outcome in
    the last WATCH_TRADE_WINDOW_SEC, on ML/spread/total markets. Shows the full summed amount."""
    now = time.time()
    if not STATE.watchlist:
        return

    for g in games:
        for cond in g["holder_market_conds"]:
            trades = get_trades(cond, limit=200)
            wm = STATE.wt_watermark.get(cond, 0)
            newest = wm
            for t in trades:
                wallet = (t.get("proxyWallet") or "")
                if wallet.lower() not in STATE.watchlist:
                    continue
                ts = int(t.get("timestamp", 0) or 0)
                if ts < wm - WATCH_TRADE_WINDOW_SEC:
                    continue
                asset = str(t.get("asset", ""))
                size = float(t.get("size", 0) or 0)
                side = str(t.get("side", "")).upper()
                txh = t.get("transactionHash", "")
                fp = f"{txh}|{wallet}|{asset}|{side}|{size}"
                if fp in STATE.wt_seen_set:
                    continue
                STATE.wt_seen_set.add(fp)
                STATE.wt_seen.append(fp)
                if len(STATE.wt_seen) == STATE.wt_seen.maxlen:
                    STATE.wt_seen_set.discard(STATE.wt_seen.popleft())
                if ts <= wm:
                    continue
                newest = max(newest, ts)
                usd = t.get("usdcSize")
                price = float(t.get("price", 0) or 0)
                usd = float(usd) if usd is not None else size * price
                tmeta = g["token_meta"].get(asset, {})
                STATE.wt_buffer.append((ts, wallet, asset, side, usd, {
                    "name": t.get("name") or t.get("pseudonym") or (wallet[:6] + "..." + wallet[-4:]),
                    "wallet": wallet,
                    "outcome": t.get("outcome") or tmeta.get("outcome", "?"),
                    "mtype": tmeta.get("mtype", "other"),
                    "label": tmeta.get("label", g["label"]),
                    "slug": g["slug"],
                }))
            STATE.wt_watermark[cond] = newest

    # purge old buffer + expired locks
    while STATE.wt_buffer and now - STATE.wt_buffer[0][0] > WATCH_TRADE_WINDOW_SEC:
        STATE.wt_buffer.popleft()
    for k in [k for k, exp in STATE.wt_alerted.items() if exp < now]:
        STATE.wt_alerted.pop(k, None)

    # aggregate by (wallet, asset, side) -> full summed USD
    agg = defaultdict(lambda: [0.0, None])
    for ts, wallet, asset, side, usd, meta in STATE.wt_buffer:
        key = (wallet, asset, side)
        agg[key][0] += usd
        agg[key][1] = meta
    for (wallet, asset, side), (total, meta) in agg.items():
        if total < WATCH_TRADE_USD:
            continue
        if STATE.wt_alerted.get((wallet, asset, side), 0) > now:
            continue
        STATE.wt_alerted[(wallet, asset, side)] = now + WATCH_TRADE_WINDOW_SEC
        icon = "🟢" if side == "BUY" else "🔴"
        verb = "bought" if side == "BUY" else "sold"
        btype = BET_TYPE_LABEL.get(meta["mtype"], meta["mtype"].title())
        name_link = f'<a href="{pm_profile_url(meta["wallet"])}">{esc(meta["name"])}</a>'
        msg = (
            f"{icon} <b>Top-holder {side}</b>\n"
            f"{name_link} {verb} ≈ <b>${total:,.0f}</b>\n"
            f"{btype}: {esc(meta['outcome'])}\n"
            f"{esc(meta['label'])}\n"
            f'<a href="{pm_game_url(meta["slug"])}">Open game</a>'
        )
        tg_send(msg)


# =========================== Main loop ===========================
def run_diag():
    """Find the World Cup tag, list today's games and their markets.
    Run:  ./venv/bin/python worldcup_polymarket_bot.py --diag
    """
    today = datetime.now(timezone.utc).date()
    print("today (UTC):", today)

    print("\n===== /sports entries matching world/fifa/soccer =====")
    sports = http_get(f"{GAMMA}/sports")
    if isinstance(sports, list):
        print("total sports:", len(sports))
        for sp in sports:
            if not isinstance(sp, dict):
                continue
            slug = str(sp.get("sport", "")).lower()
            if any(k in slug for k in ("world", "fifa", "fifwc", "soccer", "football")):
                print("  sport=%s tags=%s series=%s" %
                      (sp.get("sport"), sp.get("tags"), sp.get("series")))

    print("\n===== candidate tag ids from /sports (with real match counts) =====")
    cands = candidate_tag_ids_from_sports()
    print("candidates:", cands)
    for tid in cands:
        n, _ = count_main_games(tid)
        print("  tag %s -> %d real match event(s)" % (tid, n))

    print("\n===== resolved tag via discover_tag_id() =====")
    tid = discover_tag_id()
    print("resolved tag id:", tid)

    if tid:
        evs = fetch_tag_events(tid)
        mains = [e for e in evs if is_main_game_event(e)]
        print("events under tag: %d  (real matches: %d)" % (len(evs), len(mains)))
        window_hi = today + timedelta(days=DAYS_AHEAD)
        print("date window: %s .. %s  (DAYS_AHEAD=%d)" % (today, window_hi, DAYS_AHEAD))
        mains.sort(key=lambda e: str(e.get("slug", "")))
        in_window = []
        for ev in mains:
            slug = ev.get("slug", "")
            gd = game_date_from_slug(slug)
            mark = ""
            if gd is not None and today <= gd <= window_hi:
                mark = "  <<< IN WINDOW"
                in_window.append(ev)
            print(f"  {slug}  game_date={gd}  closed={ev.get('closed')}{mark}")
        print("\nmatches in window:", len(in_window))
        for ev in in_window[:6]:
            print(f"\n  -- {ev.get('slug')} --")
            for m in (ev.get("markets") or [])[:30]:
                print(f"     [{classify_market(m)}] vol={market_volume(m):.0f} "
                      f"cond={m.get('conditionId')} outcomes={parse_json_array(m.get('outcomes'))}")
            g = build_token_meta(ev)
            print("     -> holder markets (ML + main spread + main total):",
                  g["holder_market_conds"])
            mt = {}
            for tok, meta in g["token_meta"].items():
                if meta["conditionId"] in g["holder_market_conds"]:
                    mt.setdefault(meta["mtype"], []).append(meta["outcome"])
            print("     -> tracked outcomes by type:", dict(mt))
        print("\nTip: to also watch upcoming days, set DAYS_AHEAD=3 in /etc/wc-bot.env")
        if in_window:
            print("Force-run line if needed:")
            print("  GAMES_SLUGS=" + ",".join(e.get("slug", "") for e in in_window))
    else:
        print("No tag resolved. Raw first /sports object for reference:")
        sports = http_get(f"{GAMMA}/sports")
        if isinstance(sports, list) and sports:
            print(json.dumps(sports[0], indent=2)[:1500])

    print("\nDONE. Paste this whole output back.")


def main():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.error("Set TG_BOT_TOKEN and TG_CHAT_ID (env vars) before running.")
        sys.exit(1)

    log.info("Starting World Cup Polymarket bot. ref=%s", REF_CODE)
    tg_send("🤖 <b>World Cup Polymarket bot started</b>\nWatching today's matches.")

    games = []
    last_games_refresh = 0.0
    last_holders = 0.0

    while True:
        now = time.time()

        # refresh today's games
        if now - last_games_refresh >= GAMES_REFRESH_SEC or not games:
            try:
                games = get_today_games()
                last_games_refresh = now
                if games:
                    log.info("Tracking %d game(s):", len(games))
                    for g in games:
                        log.info("  - %s | all_markets=%d holder_markets=%d moneyline_tokens=%d slug=%s",
                                 g["label"], len(g["condition_ids"]),
                                 len(g["holder_market_conds"]),
                                 len(g["moneyline_tokens"]), g["slug"])
                else:
                    log.info("No World Cup games found in date window (UTC). Will retry.")
            except Exception as e:
                log.exception("game refresh failed: %s", e)

        if games:
            # refresh watchlist + whales first (so watched-trades has a list to work with)
            if now - last_holders >= HOLDERS_POLL_SEC or not STATE.watchlist:
                try:
                    refresh_holders_and_whales(games)
                except Exception as e:
                    log.exception("holders/whales refresh failed: %s", e)
                last_holders = now

            # every tick (1 min): odds moves, smart money (anyone), watched-holder trades
            try:
                detect_price_moves(games)
            except Exception as e:
                log.exception("price move detector failed: %s", e)
            try:
                detect_smart_money(games)
            except Exception as e:
                log.exception("smart money detector failed: %s", e)
            try:
                detect_watched_trades(games)
            except Exception as e:
                log.exception("watched-trades detector failed: %s", e)

        time.sleep(TICK_SEC)


if __name__ == "__main__":
    if "--diag" in sys.argv:
        run_diag()
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")

# ============================================================================
# systemd unit (example) — /etc/systemd/system/wc-bot.service
# ----------------------------------------------------------------------------
# [Unit]
# Description=World Cup Polymarket Telegram bot
# After=network-online.target
#
# [Service]
# WorkingDirectory=/opt/wc-bot
# Environment=TG_BOT_TOKEN=123456:ABC...
# Environment=TG_CHAT_ID=-1001234567890
# ExecStart=/opt/wc-bot/venv/bin/python /opt/wc-bot/worldcup_polymarket_bot.py
# Restart=always
# RestartSec=10
#
# [Install]
# WantedBy=multi-user.target
# ----------------------------------------------------------------------------
# sudo systemctl daemon-reload && sudo systemctl enable --now wc-bot
# journalctl -u wc-bot -f       # смотреть логи
# ============================================================================
