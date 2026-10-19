#!/usr/bin/env python3
"""WICK HUNTER market-wide ticker radar + two-stage experimental alerts.

Uses official *public* OKX REST data and sends no orders. Prices and
candles belong ONLY to OKX; KCEX listing and prices are NOT verified.

Manual test: python scanner_dual_alert.py
Offline test: python scanner_market_radar.py --demo
Dry-run market check: python scanner_market_radar.py --dry-run
"""
import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://www.okx.com"
STATE_PATH = Path(os.getenv("RADAR_STATE_FILE", "state_radar.json"))
REPORT_PATH = Path(os.getenv("RADAR_REPORT_FILE", "report_radar.json"))
MAX_DEEP_SCAN = min(90, max(10, int(os.getenv("MAX_DEEP_SCAN", "60"))))
MIN_TURNOVER = float(os.getenv("MIN_24H_USDT", "150000"))
MIN_GROWTH_1H = float(os.getenv("MIN_1H_PCT", "5"))
MIN_RVOL = float(os.getenv("MIN_RVOL", "1.5"))
NEAR_PCT = float(os.getenv("NEAR_PCT", "4"))
BREAK_WINDOW_MIN = max(5, min(90, int(os.getenv("BREAK_WINDOW_MIN", "30"))))
ZONE_HOURS = max(1, float(os.getenv("ZONE_HOURS", "6")))


class MarketDataError(Exception):
    pass


@dataclass(frozen=True)
class Candle:
    t: int  # UTC unix seconds, candle OPEN time
    o: float
    h: float
    l: float
    c: float
    v: float  # quote turnover in USDT for SWAP, not contract count


def num(value, default=float("nan")):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def get_json(path, query=None):
    url = API_BASE + path
    if query:
        url += "?" + urlencode(query)
    req = Request(url, headers={"User-Agent": "WickHunter-OKX-Research/1.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except (URLError, HTTPError, TimeoutError, ValueError, OSError) as exc:
        raise MarketDataError(f"OKX network error at {path}: {type(exc).__name__}: {exc}") from exc


def unwrap(data):
    if not isinstance(data, dict) or str(data.get("code")) != "0" or not isinstance(data.get("data"), list):
        raise MarketDataError(f"OKX API rejected or changed schema: {str(data)[:180]}")
    return data["data"]


def parse_instruments(data):
    rows = unwrap(data)
    # instCategory is the official OKX asset class: 1=crypto, 3=stocks,
    # 4=commodities, 5=FX, 6=bonds. Fail closed for missing/unknown values.
    # This prevents stock/ETF perpetuals (e.g. SOXL) from being treated as altcoins.
    return {str(row.get("instId", "")) for row in rows
            if isinstance(row, dict) and row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and str(row.get("instCategory", "")) == "1"
            and str(row.get("instId", "")).endswith("-USDT-SWAP")}


def parse_tickers(data, active, now):
    if not active:
        raise MarketDataError("OKX instrument list empty; aborting")
    out = []
    for row in unwrap(data):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("instId", ""))
        if symbol not in active:
            continue
        price = num(row.get("last"))
        open24 = num(row.get("open24h"))
        basevol = num(row.get("volCcy24h"), 0.0)
        bid = num(row.get("bidPx"))
        ask = num(row.get("askPx"))
        ts = int(num(row.get("ts"), 0)) // 1000
        if not (price > 0 and open24 > 0 and basevol > 0 and ts >= now - 300 and ts <= now + 60):
            continue
        spread = (ask / bid - 1) * 100 if bid > 0 and ask >= bid else float("inf")
        if spread > 1.5:
            continue
        # OKX *derivative* volCcy24h is base-coin units: convert approx to USDT.
        turnover = basevol * price
        if turnover < MIN_TURNOVER:
            continue
        out.append({"symbol": symbol, "price": price, "turnover_est_usdt": round(turnover),
                    "growth24h": round(100 * (price / open24 - 1), 2),
                    "high24h": num(row.get("high24h")),
                    "low24h": num(row.get("low24h")),
                    "spread_pct": round(spread, 3)})
    if not out:
        raise MarketDataError("No fresh, liquid USDT perpetual tickers from OKX. Aborting; check timestamp/source.")
    return out


def parse_candles(data, now, seconds, min_count):
    candles = []
    for item in unwrap(data):
        if not isinstance(item, list) or len(item) < 9 or str(item[8]) != "1":
            continue
        t = int(num(item[0], 0)) // 1000
        o, h, l, c, quotevol = (num(item[i]) for i in (1, 2, 3, 4, 7))
        if (t <= 0 or not all(math.isfinite(x) for x in (o, h, l, c, quotevol))
                or min(o, h, l, c) <= 0 or quotevol < 0 or h < max(o, c, l)
                or l > min(o, c, h) or t + seconds > now + 2):
            continue
        candles.append(Candle(t, o, h, l, c, quotevol))
    candles = sorted({b.t: b for b in candles}.values(), key=lambda b: b.t)
    if len(candles) < min_count:
        raise MarketDataError(f"Not enough confirmed closed candles: {len(candles)} < {min_count}")
    if now - (candles[-1].t + seconds) > seconds * 3 + 60:
        raise MarketDataError("Stale candles; refusing to infer current conditions")
    return candles


class OKX:
    def symbols(self, now):
        live = parse_instruments(get_json("/api/v5/public/instruments", {"instType": "SWAP"}))
        return parse_tickers(get_json("/api/v5/market/tickers", {"instType": "SWAP"}), live, now)

    def candles(self, symbol, interval, now):
        if interval not in ("15m", "1m") or not symbol.endswith("-USDT-SWAP"):
            raise MarketDataError("Invalid symbol/timeframe")
        dur = 900 if interval == "15m" else 60
        count = 80 if interval == "15m" else 20
        rows = get_json("/api/v5/market/candles", {"instId": symbol, "bar": interval,
                                                       "limit": "200" if interval == "15m" else "120"})
        return parse_candles(rows, now, dur, count)


def mean(seq):
    return sum(seq) / len(seq) if seq else 0.0


def volatility(bars):
    tr = [max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
          for i, b in enumerate(bars) if i > 0]
    return mean(tr[-14:])


def pump_metrics(bars):
    if len(bars) < 25:
        return None
    growth = (bars[-1].c / bars[-5].c - 1) * 100
    avgvol = mean([b.v for b in bars[-21:-1]])
    rvol = bars[-1].v / avgvol if avgvol > 0 else 0
    return {"growth_1h_pct": round(growth, 3), "relative_volume": round(rvol, 3)}


def candidate_zone(bars, price):
    if len(bars) < 80:
        return None
    atr = volatility(bars)
    if atr <= 0:
        return None
    historical = bars[-165:-4]  # excludes last four bars from resistance search
    peaks = []
    for i in range(3, len(historical) - 3):
        high = historical[i].h
        if (high > max(b.h for b in historical[i - 3:i])
                and high >= max(b.h for b in historical[i + 1:i + 4])
                and price * 1.003 < high < price * 1.35):
            peaks.append(high)
    if peaks:
        center, kind = min(peaks), "HISTORICAL_PIVOT"
    else:
        center, kind = price + 2.7 * atr, "ATR_GUESS_LOW_CONFIDENCE"
    half_width = max(0.55 * atr, 0.004 * center)
    return {"low": center - half_width, "high": center + half_width,
            "source": kind}


def select_candidates(tickers, zones, now, budget=60):
    """Full-universe ticker screen, then balanced capped candle shortlist.

    PUMP: large 24h gainers. EARLY: moderate gainers near their 24h high
    with meaningful range. TRACKED: still-live zones from previous runs.
    This does NOT fetch candlesticks for every market symbol.
    """
    valid = {t["symbol"]: t for t in tickers}
    tracked = sorted((s for s, z in zones.items()
                      if s in valid and z.get("expires", 0) > now))
    def spread_score(t):
        return max(0.0, 1.5 - t["spread_pct"])

    pump = sorted((t for t in tickers if t["growth24h"] >= 5),
                  key=lambda t: (t["growth24h"] + .75 * spread_score(t),
                                 t["turnover_est_usdt"]), reverse=True)
    early = []
    for t in tickers:
        low, high, price = t["low24h"], t["high24h"], t["price"]
        if not (low > 0 and high > low and 0 < price <= high * 1.005):
            continue
        range_pct = (high / low - 1) * 100
        at_top = (price - low) / (high - low)
        # No 24h mega-pump required, but avoid flat / illiquid noise.
        if (-3 <= t["growth24h"] < 5 and range_pct >= 4
                and at_top >= 0.65):
            score = 3 * at_top + 0.12 * min(range_pct, 35) + spread_score(t)
            early.append((score, t))
    early.sort(key=lambda st: (st[0], st[1]["turnover_est_usdt"]), reverse=True)

    chosen, groups = [], {}
    def add(symbol, group):
        if symbol in valid and symbol not in groups and len(chosen) < budget:
            chosen.append(valid[symbol])
            groups[symbol] = group
    # Preserve any ongoing WATCH/BREAK state before new candidates.
    for symbol in tracked:
        add(symbol, "TRACKED")
    remaining = max(0, budget - len(chosen))
    pump_quota = int(remaining * 0.6 + .5)
    early_quota = remaining - pump_quota
    for t in pump[:pump_quota]:
        add(t["symbol"], "PUMP")
    for _, t in early[:early_quota]:
        add(t["symbol"], "EARLY")
    # Fill unused slots from either bucket, no duplicate symbols.
    for t in pump:
        add(t["symbol"], "PUMP")
    for _, t in early:
        add(t["symbol"], "EARLY")
    counts = {"ticker_screened": len(tickers), "pump_candidates": len(pump),
              "early_candidates": len(early), "tracked_active": len(tracked),
              "deep_scan_count": len(chosen)}
    return chosen, groups, counts


def mini_break(bars, zone, watched_at, checked_through=0):
    """Find first confirmed 1m trend break after WATCH (even between scans).

    Pivot lows need two candles *after* each pivot, available before the
    candidate break candle; there is no look-ahead / future-candle leakage.
    """
    if len(bars) < 20 or not watched_at:
        return None
    for j in range(7, len(bars)):
        cur = bars[j]
        event_time = cur.t + 60  # close timestamp, not candle open
        if event_time <= max(checked_through, watched_at):
            continue
        if event_time > watched_at + BREAK_WINDOW_MIN * 60:
            continue
        prev = bars[j - 1]
        # Candidate pivots can only be confirmed using candles already closed
        # strictly before the break candle itself (j).
        pivots = []
        for i in range(max(2, j - 40), j - 2):
            if (bars[i].l < min(b.l for b in bars[i - 2:i])
                    and bars[i].l <= min(b.l for b in bars[i + 1:i + 3])):
                pivots.append(i)
        if len(pivots) < 2:
            continue
        a, b = pivots[-2:]
        if not (0 < b - a <= 25 and bars[b].l > bars[a].l):
            continue
        slope = (bars[b].l - bars[a].l) / (b - a)
        support_prev = bars[a].l + slope * (j - 1 - a)
        support_now = bars[a].l + slope * (j - a)
        if prev.c >= support_prev and cur.c < support_now and cur.c < prev.l:
            return {"price": cur.c, "at": event_time,
                    "pivot_a_at": bars[a].t, "pivot_b_at": bars[b].t,
                    "support_at_break": support_now}
    return None


def is_zone_approach(bar, zone):
    """Tests whether a 1m closed candle touched the configured watch range."""
    return (bar.h >= zone["low"] * (1 - NEAR_PCT / 100)
            and bar.l <= zone["high"] * 1.015)

def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf8"))
            if isinstance(data, dict) and isinstance(data.get("zones"), dict):
                return data
        except (OSError, ValueError):
            pass
    return {"zones": {}, "seen": {}}


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf8")


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    endpoint = "https://api.telegram.org/bot" + token + "/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf8")
    req = Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.load(resp)
            return bool(data.get("ok"))
    except (OSError, ValueError):
        print("Telegram delivery error (token omitted)")
        return False


def fmt(n):
    return f"{n:.8f}".rstrip("0").rstrip(".") if n < 1 else f"{n:.5f}".rstrip("0").rstrip(".")


def run(source, now, offline=False, dry_run=False):
    state = {"zones": {}, "seen": {}} if (offline or dry_run) else load_state()
    zones, seen = state["zones"], state.setdefault("seen", {})
    tickers = source.symbols(now)
    ticker_map = {t["symbol"]: t for t in tickers}
    for old_symbol in list(zones):
        if old_symbol not in ticker_map:
            zones.pop(old_symbol, None)

    # Screen *all eligible* crypto tickers cheaply, then fetch expensive
    # 15m/1m candles only for capped, ranked candidates.
    selected, groups, screen_counts = select_candidates(tickers, zones, now, MAX_DEEP_SCAN)
    wanted = [row["symbol"] for row in selected]
    group_of = {row["symbol"]: groups[row["symbol"]] for row in selected}

    results, errors, messages = [], {}, []
    good_candles, break_count = 0, 0
    for sym in wanted:
        ticker = ticker_map[sym]
        try:
            bars = source.candles(sym, "15m", now)
            good_candles += 1
            metric = pump_metrics(bars)
            if not metric:
                continue
            z = zones.get(sym)
            if z and (z.get("expires", 0) <= now or
                      bars[-1].c > z["high"] + volatility(bars)):
                zones.pop(sym, None)
                z = None
            # PUMP needs fast growth+volume; EARLY needs milder 1h growth
            # and an *actual historical pivot* (never invent an ATR ceiling).
            strong = metric["growth_1h_pct"] >= MIN_GROWTH_1H and metric["relative_volume"] >= MIN_RVOL
            emerging = (group_of.get(sym) == "EARLY"
                        and metric["growth_1h_pct"] >= 1.5
                        and metric["relative_volume"] >= 1.15)
            if z is None and (strong or emerging):
                candidate = candidate_zone(bars, bars[-1].c)
                if candidate and (strong or candidate["source"] == "HISTORICAL_PIVOT"):
                    z = candidate
                    z.update({"created": now, "expires": now + int(ZONE_HOURS * 3600),
                              "category": "PUMP" if strong else "EARLY"})
                    zones[sym] = z
            if not z:
                continue

            price = ticker["price"]
            dist = 100 * (z["low"] - price) / z["low"]
            near_now = -1.25 <= dist <= NEAR_PCT and price <= z["high"] * 1.015
            # A separate 1m check runs for every active zone, not just when
            # the latest ticker is inside the zone. A break after leaving
            # the zone will otherwise be missed by a 5-minute poll.
            bars1 = []
            try:
                bars1 = source.candles(sym, "1m", now)
            except MarketDataError as exc:
                errors[sym + ":1m"] = str(exc)[:150]

            recent_touches = [b.t + 60 for b in bars1
                              if b.t + 60 >= z["created"]
                              and 0 <= now - (b.t + 60) <= 600
                              and is_zone_approach(b, z)]
            # Preserve the earliest recently observed approach, not the most
            # recent: otherwise a break after approach is wrongly rejected.
            first_recent_touch = min(recent_touches, default=0)
            if (near_now or first_recent_touch) and not z.get("watch_at"):
                z["watch_at"] = first_recent_touch or now

            watch_key = f"{sym}:WATCH:{z['created']}"
            if z.get("watch_at") and watch_key not in seen:
                messages.append((watch_key,
                    f"🟡 WATCH — OKX research only, NOT an entry\n"
                    f"{sym}\nOKX price {fmt(price)} | experimental resistance "
                    f"{fmt(z['low'])}–{fmt(z['high'])}\n"
                    f"Wait for a separate MINI BREAK alert. KCEX listing/price NOT checked."))

            break_event = None
            # ONE break alert max per zone. Only closed 1m candles, and only
            # after a recorded WATCH; not a claim about KCEX trend structure.
            if z.get("watch_at") and not z.get("break_at") and bars1:
                break_event = mini_break(bars1, z, z["watch_at"], z.get("checked_through", 0))
                if break_event:
                    z["break_at"] = break_event["at"]
                    z["break_price"] = break_event["price"]
                    break_count += 1
                z["checked_through"] = bars1[-1].t + 60
            if z.get("break_at"):
                break_key = f"{sym}:BREAK:{z['created']}:{z['break_at']}"
                if break_key not in seen:
                    when = datetime.fromtimestamp(z["break_at"], timezone.utc).strftime('%H:%M UTC')
                    messages.append((break_key,
                        f"🔴 MINI BREAK — OKX research only, NOT a trade signal\n"
                        f"{sym} | confirmed 1m close {fmt(z['break_price'])} at {when}\n"
                        f"Earlier WATCH zone {fmt(z['low'])}–{fmt(z['high'])}\n"
                        f"This can arrive late on GitHub Actions. KCEX listing/price NOT checked."))

            entry = {"symbol_okx": sym, "price_okx": price,
                     "growth24h_pct_okx": ticker["growth24h"], **metric,
                     "zone_low_okx": z["low"], "zone_high_okx": z["high"],
                     "zone_source": z["source"],
                     "screen_group": group_of.get(sym, "TRACKED"),
                     "distance_pct": round(dist, 2),
                     "watch": bool(z.get("watch_at")), "near_now": near_now,
                     "mini_break": bool(z.get("break_at")),
                     "last_break_utc": (datetime.fromtimestamp(z["break_at"], timezone.utc).isoformat()
                                        if z.get("break_at") else None),
                     "kcex_listing_and_price_checked": False}
            results.append(entry)
        except MarketDataError as exc:
            errors[sym] = str(exc)[:150]
        if not offline:
            time.sleep(0.18)

    results.sort(key=lambda row: (not row["near_now"], abs(row["distance_pct"])))
    report = {"source": "OKX official public data — crypto-only USDT perpetual (instCategory=1)",
              "experimental_capped_candle_scan": True,
              "selection": screen_counts,
              "selected_symbols": [{"symbol": x["symbol"], "category": group_of[x["symbol"]]} for x in selected],
              "NOT_KCEX_PRICES": True,
              "created_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
              "tickers_eligible": len(tickers), "pairs_scanned": len(wanted),
              "valid_15m_series": good_candles,
              "watch_near_now": sum(bool(r["near_now"]) for r in results),
              "mini_breaks_detected_this_scan": break_count,
              "errors": errors, "results": results[:80]}
    save_json(REPORT_PATH, report)
    if wanted and good_candles == 0:
        raise MarketDataError("Every OKX 15m candle request failed; see report_okx.json")
    print(f"{'OFFLINE DEMO' if offline else 'OKX MARKET RADAR READ OK'}: "
          f"{len(tickers)} crypto swaps screened via tickers; "
          f"PUMP candidates {screen_counts['pump_candidates']}; "
          f"EARLY candidates {screen_counts['early_candidates']}; "
          f"candle scanned {len(wanted)}; valid 15m "
          f"{good_candles}; WATCH near now {report['watch_near_now']}; "
          f"MINI BREAK this scan {break_count}; errors {len(errors)}")
    for row in results[:15]:
        print(f"{row['symbol_okx']:22} {fmt(row['price_okx']):>13} "
              f"zone {fmt(row['zone_low_okx'])}–{fmt(row['zone_high_okx'])} "
              f"WATCH={row['watch']} BREAK={row['mini_break']}")
    for key, msg in messages:
        print(msg)
        if dry_run:
            print("DRY RUN: Telegram not called; no alert state saved")
        elif offline or send_telegram(msg):
            seen[key] = now
    state["seen"] = {k: v for k, v in seen.items() if now - v < 172800}
    state["zones"] = {k: v for k, v in zones.items() if v.get("expires", 0) > now}
    if not offline and not dry_run:
        save_json(STATE_PATH, state)
    return report


class OfflineDemo:
    def symbols(self, now):
        return [{"symbol": "DEMO-USDT-SWAP", "price": .066, "turnover_est_usdt": 500000,
                 "growth24h": 35, "spread_pct": .3}]

    def candles(self, symbol, interval, now):
        seconds = 900 if interval == "15m" else 60
        n = 180 if interval == "15m" else 100
        t0 = (now // seconds - n) * seconds
        candles = []
        for i in range(n):
            price = .050 + i * .0001
            if i == 80:
                price = .071
            candles.append(Candle(t0 + i * seconds, price - .0002, price + .0004,
                                  price - .0006, price, 15000 if i >= n - 2 else 4000))
        return candles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="synthetic OFFLINE example; NOT live prices")
    parser.add_argument("--dry-run", action="store_true", help="read live OKX but NEVER message Telegram or save state")
    args = parser.parse_args()
    try:
        run(OfflineDemo() if args.demo else OKX(), int(time.time()), offline=args.demo, dry_run=args.dry_run)
    except MarketDataError as exc:
        print("DATA ERROR:", exc, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
