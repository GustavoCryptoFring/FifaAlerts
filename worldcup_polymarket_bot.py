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
import sys
import json
import time
import html
import logging
from collections import deque, defaultdict
from datetime import datetime, timezone, date

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

SMART_MONEY_USD        = float(os.getenv("SMART_MONEY_USD", "10000"))   # $10k
SMART_MONEY_WINDOW_SEC = int(os.getenv("SMART_MONEY_WINDOW_SEC", "300"))# суммировать за 5 минут

WHALE_USD = float(os.getenv("WHALE_USD", "100000"))   # $100k в одной позиции

TOP_HOLDERS_N          = int(os.getenv("TOP_HOLDERS_N", "10"))      # топ-N держателей на исход
HOLDER_MIN_DELTA_SHARES= float(os.getenv("HOLDER_MIN_DELTA_SHARES", "1"))  # игнорировать дребезг < N shares

# --- Poll intervals (интервалы опроса) ---
PRICE_POLL_SEC   = int(os.getenv("PRICE_POLL_SEC", "15"))    # цены/сделки
HOLDERS_POLL_SEC = int(os.getenv("HOLDERS_POLL_SEC", "180")) # держатели/киты
GAMES_REFRESH_SEC= int(os.getenv("GAMES_REFRESH_SEC", "600"))# обновление списка сегодняшних игр

# --- Game discovery (поиск игр) ---
# Если автопоиск тега не сработает на твоём VPS — задай WORLD_CUP_TAG_ID
# или явный список слагов событий через GAMES_SLUGS="fifwc-che-can-2026-06-24,fifwc-bih-qat-2026-06-24"
WORLD_CUP_TAG_ID = os.getenv("WORLD_CUP_TAG_ID", "")
GAMES_SLUGS      = [s.strip() for s in os.getenv("GAMES_SLUGS", "").split(",") if s.strip()]
EVENT_SLUG_PREFIX= os.getenv("EVENT_SLUG_PREFIX", "fifwc")  # слаг игр World Cup начинается с этого

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
def classify_market(m):
    """Return 'moneyline' | 'spread' | 'total' | 'other' using several heuristics."""
    for key in ("sportsMarketType", "marketType", "type"):
        v = str(m.get(key, "")).lower()
        if v in ("moneyline", "spreads", "spread", "totals", "total"):
            return {"spreads": "spread", "totals": "total"}.get(v, v)
    text = (str(m.get("slug", "")) + " " + str(m.get("question", "")) + " " +
            str(m.get("groupItemTitle", ""))).lower()
    if "moneyline" in text:
        return "moneyline"
    if "spread" in text:
        return "spread"
    if "total" in text or "over/under" in text or "o/u" in text:
        return "total"
    # Fallback: a 3-way market containing a "draw" outcome is the match winner.
    outs = [str(o).lower() for o in parse_json_array(m.get("outcomes"))]
    if any("draw" in o for o in outs):
        return "moneyline"
    return "other"


def build_token_meta(event):
    """
    From one game event build:
      - game label and slug
      - per-token metadata (outcome name, market type, conditionId)
      - list of moneyline token ids (for price-move detection)
      - list of condition ids (for trades & holders)
    """
    slug = event.get("slug", "")
    label = event.get("title") or slug
    token_meta = {}         # tokenId -> {outcome, mtype, conditionId, slug, label}
    moneyline_tokens = []   # [tokenId, ...]
    condition_ids = []      # [conditionId, ...]
    token_to_condition = {} # tokenId -> conditionId

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
            token_to_condition[tok] = cond
            if mtype == "moneyline":
                moneyline_tokens.append(tok)

    return {
        "slug": slug,
        "label": label,
        "token_meta": token_meta,
        "moneyline_tokens": moneyline_tokens,
        "condition_ids": list(dict.fromkeys(condition_ids)),
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


def tag_has_world_cup_events(tag_id):
    """A tag is the right one only if it actually contains fifwc- game events."""
    evs = http_get(f"{GAMMA}/events", params={
        "tag_id": tag_id, "closed": "false", "active": "true", "limit": 50})
    if not isinstance(evs, list):
        return False
    return any(isinstance(e, dict) and str(e.get("slug", "")).startswith(EVENT_SLUG_PREFIX)
               for e in evs)


def discover_tag_id():
    """Find the World Cup tag id by scanning all tags and validating via events. Cached."""
    global _TAG_CACHE
    if WORLD_CUP_TAG_ID:
        return WORLD_CUP_TAG_ID
    if _TAG_CACHE != "UNSET":
        return _TAG_CACHE
    result = None
    try:
        candidates = [t for t in iter_tags() if looks_world_cup(t)]
        # prefer an exact 'world-cup' slug, then non-"club" world cup labels
        candidates.sort(key=lambda t: (
            0 if str(t.get("slug", "")).lower() == "world-cup" else
            1 if ("world" in str(t.get("label", "")).lower() and "club" not in str(t.get("label", "")).lower()) else 2
        ))
        for t in candidates:
            tid = str(t.get("id"))
            if tag_has_world_cup_events(tid):
                log.info("World Cup tag resolved: id=%s slug=%s label=%s",
                         tid, t.get("slug"), t.get("label"))
                result = tid
                break
    except Exception as e:
        log.warning("tag discovery failed (%s); will fall back to slug scan", e)
    if result is None:
        log.warning("No validated World Cup tag found. Using slug scan; "
                    "set WORLD_CUP_TAG_ID or GAMES_SLUGS to be safe.")
    else:
        _TAG_CACHE = result   # cache only successful resolutions
    return result


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


def get_today_games():
    """Return list of game dicts (see build_token_meta) for today's World Cup matches."""
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
        if not str(slug).startswith(EVENT_SLUG_PREFIX) and not GAMES_SLUGS:
            continue
        if not GAMES_SLUGS and not is_today_utc(ev):
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
        # holders
        self.prev_amounts = defaultdict(dict)            # token -> {wallet: amount}
        self.whales = set()                              # (token, wallet) currently >= WHALE_USD

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
        msg = (
            f"{icon} <b>Smart money — {side}</b>\n"
            f"<b>{esc(meta['name'])}</b> {verb} <b>{esc(meta['outcome'])}</b>\n"
            f"{esc(meta['label'])}\n"
            f"≈ ${total:,.0f} in ≤{SMART_MONEY_WINDOW_SEC//60}m\n"
            f'<a href="{link}">Open game</a>'
        )
        tg_send(msg)


def detect_holders(games):
    now = time.time()
    price_cache = {}
    for g in games:
        for cond in g["condition_ids"]:
            blocks = get_holders(cond, limit=200)
            for block in blocks:
                token = block.get("token")
                holders = block.get("holders") or []
                # normalize amounts
                rows = []
                for h in holders:
                    amt = h.get("amount", h.get("shares"))
                    try:
                        amt = float(amt)
                    except Exception:
                        continue
                    rows.append((h.get("proxyWallet") or h.get("wallet") or "", amt, h))
                if not rows:
                    continue
                rows.sort(key=lambda x: x[1], reverse=True)

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
                                msg = (
                                    f"🐋 <b>Whale position</b>\n"
                                    f"<b>{esc(holder_name(h))}</b> holds "
                                    f"{amt:,.0f} shares of <b>{esc(outcome)}</b>\n"
                                    f"{esc(label)} @ {price*100:.1f}% ≈ <b>${value:,.0f}</b>\n"
                                    f"Strong conviction on this outcome.\n"
                                    f'<a href="{link}">Open game</a>'
                                )
                                tg_send(msg)
                        else:
                            STATE.whales.discard(key)

                # ---- (3) top-10 holder buy/sell deltas ----
                prev = STATE.prev_amounts[str(token)]
                cur_top = rows[:TOP_HOLDERS_N]
                cur_top_wallets = {w for w, _, _ in cur_top}
                cur_amount = {w: a for w, a, _ in rows}
                name_by_wallet = {w: holder_name(h) for w, _, h in rows}

                # union of current top-10 and previous top-10 to catch sell-offs
                watch = set(cur_top_wallets) | set(prev.keys())
                changes = []
                for w in watch:
                    new_a = cur_amount.get(w, 0.0)
                    old_a = prev.get(w, None)
                    if old_a is None:
                        # brand new entrant into top holders
                        if w in cur_top_wallets and new_a >= HOLDER_MIN_DELTA_SHARES:
                            changes.append((name_by_wallet.get(w, w[:6] + "..."), "bought", new_a, new_a))
                        continue
                    delta = new_a - old_a
                    if abs(delta) < HOLDER_MIN_DELTA_SHARES:
                        continue
                    if w not in cur_top_wallets and w not in prev:
                        continue
                    verb = "bought" if delta > 0 else "sold"
                    nm = name_by_wallet.get(w, w[:6] + "...")
                    changes.append((nm, verb, abs(delta), new_a))

                if changes:
                    lines = [f"📊 <b>Top holders moved</b> — <b>{esc(outcome)}</b>",
                             f"{esc(label)}"]
                    for nm, verb, qty, holding in changes[:TOP_HOLDERS_N]:
                        lines.append(f"• {esc(nm)} {verb} {qty:,.0f} (now {holding:,.0f})")
                    lines.append(f'<a href="{link}">Open game</a>')
                    tg_send("\n".join(lines))

                # store snapshot (keep only wallets we actually saw)
                STATE.prev_amounts[str(token)] = dict(cur_amount)


# =========================== Main loop ===========================
def run_diag():
    """Find the World Cup tag, list today's games and their markets.
    Run:  ./venv/bin/python worldcup_polymarket_bot.py --diag
    """
    today = datetime.now(timezone.utc).date()
    print("today (UTC):", today)

    print("\n===== searching ALL /tags for World Cup =====")
    matches = []
    for t in iter_tags():
        if looks_world_cup(t):
            matches.append(t)
    print("world-cup-like tags found:", len(matches))
    for t in matches[:40]:
        has = tag_has_world_cup_events(str(t.get("id")))
        print("  id=%s slug=%s label=%s  has_fifwc_events=%s" %
              (t.get("id"), t.get("slug"), t.get("label"), has))

    print("\n===== resolved tag via discover_tag_id() =====")
    tid = discover_tag_id()
    print("resolved tag id:", tid)

    if tid:
        evs = http_get(f"{GAMMA}/events", params={
            "tag_id": tid, "closed": "false", "active": "true", "limit": 100})
        evs = evs if isinstance(evs, list) else []
        print("events under tag:", len(evs))
        todays = []
        for ev in evs:
            sd = ev.get("startDate") or ev.get("gameStartTime")
            mark = ""
            try:
                d = datetime.fromisoformat(str(sd).replace("Z", "+00:00")).astimezone(timezone.utc).date()
                if d == today:
                    mark = "  <<< TODAY"
                    todays.append(ev)
            except Exception:
                pass
            if str(ev.get("slug", "")).startswith(EVENT_SLUG_PREFIX):
                print(f"  slug={ev.get('slug')} start={sd} closed={ev.get('closed')}{mark}")
        print("\nTODAY games count:", len(todays))
        for ev in todays[:6]:
            print(f"\n  -- markets of {ev.get('slug')} --")
            for m in (ev.get("markets") or [])[:8]:
                print(f"     [{classify_market(m)}] cond={m.get('conditionId')} "
                      f"outcomes={parse_json_array(m.get('outcomes'))} "
                      f"tokens={'yes' if m.get('clobTokenIds') else 'NO'}")
        if todays:
            print("\nForce-run line for /etc/wc-bot.env if needed:")
            print("  GAMES_SLUGS=" + ",".join(e.get("slug", "") for e in todays))
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
                    log.info("Tracking %d game(s) today:", len(games))
                    for g in games:
                        log.info("  - %s | markets=%d moneyline_tokens=%d slug=%s",
                                 g["label"], len(g["condition_ids"]),
                                 len(g["moneyline_tokens"]), g["slug"])
                else:
                    log.info("No World Cup games found for today (UTC). Will retry.")
            except Exception as e:
                log.exception("game refresh failed: %s", e)

        if games:
            # fast loop: prices + smart money
            try:
                detect_price_moves(games)
            except Exception as e:
                log.exception("price move detector failed: %s", e)
            try:
                detect_smart_money(games)
            except Exception as e:
                log.exception("smart money detector failed: %s", e)

            # slower loop: holders + whales
            if now - last_holders >= HOLDERS_POLL_SEC:
                try:
                    detect_holders(games)
                except Exception as e:
                    log.exception("holders detector failed: %s", e)
                last_holders = now

        time.sleep(PRICE_POLL_SEC)


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
