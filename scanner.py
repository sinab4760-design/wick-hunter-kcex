#!/usr/bin/env python3
"""Wick Hunter KCEX scanner — read-only, experimental, no trading or orders.

KCEX futures candle endpoint is community-inferred, NOT officially verified.
Fails closed: never substitutes another exchange silently.
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
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = os.getenv('KCEX_BASE_URL', 'https://www.kcex.com').rstrip('/')
TICKER_PATH = os.getenv('KCEX_TICKER_PATH', '/fapi/v1/contract/ticker')
KLINE_PATH = os.getenv('KCEX_KLINE_PATH', '/fapi/v1/contract/kline/{symbol}')
STATE_FILE = Path(os.getenv('STATE_FILE', 'state.json'))
REPORT_FILE = Path(os.getenv('REPORT_FILE', 'report.json'))
MAX_SYMBOLS = min(100, max(5, int(os.getenv('MAX_SYMBOLS', '50'))))
MIN_24H_USDT = float(os.getenv('MIN_24H_USDT', '150000'))
MIN_1H_PCT = float(os.getenv('MIN_1H_PCT', '5.0'))
MIN_RVOL = float(os.getenv('MIN_RVOL', '1.5'))
NEAR_PCT = float(os.getenv('NEAR_PCT', '7.0'))
ZONE_HOURS = float(os.getenv('ZONE_HOURS', '6'))
CANDLE_15M = 900
CANDLE_1M = 60


class DataSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def get_json(url, params=None, timeout=12):
    if params:
        url += '?' + urlencode(params)
    req = Request(url, headers={'User-Agent': 'WickHunterResearch/1.0 (+market-data only)', 'Accept': 'application/json'})
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise DataSourceError(f'HTTP {resp.status} for {url}')
            return json.loads(resp.read(8_000_000).decode('utf-8'))
    except (HTTPError, URLError, ValueError, TimeoutError, OSError) as e:
        raise DataSourceError(f'Data request failed: {url}: {type(e).__name__}: {e}') from e


def unwrap(payload):
    if isinstance(payload, dict):
        if payload.get('success') is False or payload.get('code') not in (None, 0, '0', 200, '200'):
            raise DataSourceError(f'Exchange rejected request: {str(payload)[:250]}')
        return payload.get('data', payload)
    return payload


def number(value, default=float('nan')):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def parse_tickers(payload):
    raw = unwrap(payload)
    if isinstance(raw, dict):
        # Some feeds wrap rows in a second key or return a symbol -> ticker map.
        raw = raw.get('list', raw.get('tickers', list(raw.values())))
    if not isinstance(raw, list):
        raise DataSourceError('KCEX ticker payload is not a list of contracts')
    parsed = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get('symbol', row.get('contractCode', ''))).upper()
        if not symbol.endswith('_USDT') and not symbol.endswith('USDT'):
            continue
        if symbol.endswith('USDT') and not symbol.endswith('_USDT'):
            symbol = symbol[:-4] + '_USDT'
        price = number(row.get('lastPrice', row.get('price')))
        turnover = number(row.get('amount24', row.get('turnover24', row.get('quoteVolume'))), 0)
        if price <= 0 or turnover < MIN_24H_USDT:
            continue
        raw_change = number(row.get('riseFallRate', row.get('changeRate')), 0)
        # rate convention is normally fraction on MEXC-style feeds; KCEX not independently verified.
        pct = raw_change * 100 if abs(raw_change) <= 2 else raw_change
        spread = 0
        bid, ask = number(row.get('bid1')), number(row.get('ask1'))
        if bid > 0 and ask > bid:
            spread = (ask / bid - 1) * 100
        if spread > 1.5:
            continue
        parsed.append({'symbol': symbol, 'price': price, 'turnover': turnover, 'change24': pct, 'spread': spread})
    if not parsed:
        raise DataSourceError('No valid USDT futures tickers. KCEX schema/API may have changed.')
    return parsed


def parse_candles(payload, now, seconds):
    raw = unwrap(payload)
    if not isinstance(raw, dict):
        raise DataSourceError('Candle endpoint returned unsupported payload: expected OHLC array object.')
    ts = raw.get('time')
    fields = [raw.get(k) for k in ('open', 'high', 'low', 'close', 'vol')]
    if not isinstance(ts, list) or any(not isinstance(f, list) for f in fields):
        raise DataSourceError('Candle schema incompatible; expected data.time/open/high/low/close/vol arrays.')
    candles = []
    for item in zip(ts, *fields):
        t = int(number(item[0], 0))
        if t > 100_000_000_000:
            t //= 1000
        o, h, l, c, v = (number(x) for x in item[1:])
        if t <= 0 or not all(math.isfinite(x) for x in (o, h, l, c, v)):
            continue
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, l, c):
            continue
        # Exclude the currently forming candle. No future information.
        if t + seconds <= now + 1:
            candles.append(Candle(t, o, h, l, c, v))
    candles = sorted({b.t: b for b in candles}.values(), key=lambda b: b.t)
    if len(candles) < 40:
        raise DataSourceError('Not enough closed candles from KCEX (need at least 40).')
    if now - candles[-1].t > seconds * 3 + 90:
        raise DataSourceError('Stale KCEX candle feed; refusing to issue live alert.')
    return candles


class LiveKCEX:
    def __init__(self):
        self.requests = 0

    def tickers(self):
        self.requests += 1
        return parse_tickers(get_json(BASE + TICKER_PATH))

    def candles(self, symbol, interval, now):
        # Community-inferred, MEXC-like route; do NOT assume it works until tested.
        assert interval in ('Min15', 'Min1')
        safe = quote(symbol, safe='_')
        path = KLINE_PATH.replace('{symbol}', safe)
        self.requests += 1
        duration = 900 if interval == 'Min15' else 60
        start = now - (duration * (195 if interval == 'Min15' else 135))
        payload = get_json(BASE + path, {'interval': interval, 'start': start, 'end': now})
        return parse_candles(payload, now, duration)


def avg(nums):
    return sum(nums) / len(nums) if nums else 0


def atr15(bars):
    sample = bars[-15:]
    trs = []
    for i in range(1, len(sample)):
        b, prev = sample[i], sample[i-1]
        trs.append(max(b.h-b.l, abs(b.h-prev.c), abs(b.l-prev.c)))
    return avg(trs)


def identify_zone(bars, price):
    """Past-only candidate; pivot-high if possible, otherwise explicitly speculative ATR."""
    if len(bars) < 75:
        return None
    atr = atr15(bars)
    if atr <= 0:
        return None
    historical = bars[-155:-4]
    pivot_peaks = []
    for i in range(3, len(historical)-3):
        v = historical[i].h
        if v > max(b.h for b in historical[i-3:i]) and v >= max(b.h for b in historical[i+1:i+4]):
            if price * 1.003 < v < price * 1.35:
                pivot_peaks.append(v)
    if pivot_peaks:
        center = min(pivot_peaks)
        kind = 'HISTORICAL_RESISTANCE'
    else:
        center = price + 2.7 * atr
        kind = 'ATR_PROJECTION_LOW_CONFIDENCE'
    half_width = max(.55 * atr, .004 * center)
    low, high = center - half_width, center + half_width
    return {'low': low, 'high': high, 'kind': kind, 'created_at': bars[-1].t + 900,
            'expires_at': bars[-1].t + 900 + int(ZONE_HOURS*3600)}


def pump_metrics(bars):
    if len(bars) < 27:
        return None
    one_hour = (bars[-1].c / bars[-5].c - 1) * 100
    old_vol = [b.v for b in bars[-21:-1]]
    rvol = bars[-1].v / avg(old_vol) if avg(old_vol) > 0 else 0
    return {'growth1h': one_hour, 'rvol': rvol}


def mini_break(bars, zone, new_after):
    """Only report fresh breaks AFTER zone was observed, never retroactively."""
    if len(bars) < 20:
        return None
    last = bars[-1]
    if last.t <= new_after:
        return None
    # Zone proximity occurred in last 25 minutes after zone creation.
    recently_near = [b for b in bars[-25:] if b.t >= new_after and b.h >= zone['low'] * .989 and b.l <= zone['high']]
    if not recently_near:
        return None
    pivots = []
    for i in range(3, len(bars)-3):
        if bars[i].l < min(b.l for b in bars[i-2:i]) and bars[i].l <= min(b.l for b in bars[i+1:i+3]):
            pivots.append(i)
    if len(pivots) < 2:
        return None
    a, b = pivots[-2:]
    if bars[b].l <= bars[a].l:
        return None
    slope = (bars[b].l - bars[a].l) / (b-a)
    prev_line = bars[a].l + slope * ((len(bars)-2)-a)
    curr_line = bars[a].l + slope * ((len(bars)-1)-a)
    prev, curr = bars[-2], bars[-1]
    if curr.t <= zone['created_at'] or curr.t <= bars[b].t + 120:
        return None
    if prev.c >= prev_line and curr.c < curr_line and curr.c < prev.l:
        return {'time': curr.t, 'price': curr.c, 'line': curr_line}
    return None


def fmt_price(p):
    return f'{p:.8f}'.rstrip('0').rstrip('.') if p < 1 else f'{p:.5f}'.rstrip('0').rstrip('.')


def telegram_send(message):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        print('Telegram not configured: only console/report output.')
        return False
    # Never print URL containing token.
    body = json.dumps({'chat_id': chat, 'text': message, 'disable_web_page_preview': True}).encode('utf-8')
    req = Request(f'https://api.telegram.org/bot{token}/sendMessage', data=body,
                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=14) as resp:
            answer = json.loads(resp.read().decode('utf-8'))
            if not answer.get('ok'):
                print(f'Telegram failed: {str(answer.get("description", "unknown"))[:120]}')
                return False
    except (OSError, ValueError) as e:
        print(f'Telegram failed: {type(e).__name__}')
        return False
    return True


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('zones'), dict):
                return data
        except (ValueError, OSError):
            pass
    return {'zones': {}, 'alerts': {}}


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


def run(provider, now, demo=False):
    state = load_state()
    state.setdefault('alerts', {})
    zones = state['zones']
    tickers = provider.tickers()
    valid = {t['symbol']: t for t in tickers}
    ranked = sorted((t for t in tickers if t['change24'] >= 5),
                    key=lambda t: (t['change24'], math.log1p(t['turnover'])), reverse=True)
    wanted = [t['symbol'] for t in ranked[:MAX_SYMBOLS]]
    # Keep monitoring previously spotted setups even if the rank declines.
    wanted += [symbol for symbol, z in zones.items() if z['expires_at'] >= now and symbol in valid and symbol not in wanted]
    results = []
    failed = {}
    candidates = 0
    notifications = []
    if not wanted:
        raise DataSourceError('No qualifying candidates; if ticker schema is correct this may be normal.')
    for symbol in wanted:
        ticker = valid[symbol]
        try:
            bars = provider.candles(symbol, 'Min15', now)
            metrics = pump_metrics(bars)
            if not metrics:
                continue
            z = zones.get(symbol)
            if z and (z['expires_at'] < now or bars[-1].c > z['high'] + 1.0*atr15(bars)):
                z = None
                zones.pop(symbol, None)
            if z is None and metrics['growth1h'] >= MIN_1H_PCT and metrics['rvol'] >= MIN_RVOL:
                z = identify_zone(bars, bars[-1].c)
                if z:
                    z['created_at'] = now  # no signal using past data before discovery
                    z['expires_at'] = now + int(ZONE_HOURS * 3600)
                    zones[symbol] = z
            if not z:
                continue
            candidates += 1
            price = ticker['price']
            dist = (z['low'] - price) / z['low'] * 100
            near = (-1.25 <= dist <= NEAR_PCT) and price <= z['high'] * 1.015
            result = {'symbol': symbol, 'price': price, 'change24': ticker['change24'],
                      'growth1h': round(metrics['growth1h'], 2), 'rvol': round(metrics['rvol'], 2),
                      'zone_low': z['low'], 'zone_high': z['high'], 'zone_source': z['kind'],
                      'dist_pct': round(dist, 2), 'near': near, 'mini_break': False}
            if near:
                key = f'{symbol}:WATCH:{z["created_at"]}'
                if key not in state['alerts']:
                    notifications.append((key, f'🟡 KCEX | زیر نظر (نه سیگنال فروش)\n{symbol}\nقیمت: {fmt_price(price)}\nمحدوده آزمایشی: {fmt_price(z["low"])} – {fmt_price(z["high"])}\nفاصله: {dist:.1f}% | رشد ۱ساعت: {metrics["growth1h"]:.1f}% | RVOL: {metrics["rvol"]:.1f}\nنوع: {z["kind"]}\n⚠️ تأیید نشده؛ معامله خودکار نیست.'))
                try:
                    bars1 = provider.candles(symbol, 'Min1', now)
                    brk = mini_break(bars1, z, z['created_at'])
                    if brk:
                        result['mini_break'] = True
                        brk_key = f'{symbol}:BREAK:{brk["time"]}'
                        if brk_key not in state['alerts']:
                            notifications.append((brk_key, f'🔴 KCEX | شکست مینی‌روندِ آزمایشی\n{symbol}\nقیمت بسته‌شدن: {fmt_price(brk["price"])}\nمحدوده: {fmt_price(z["low"])} – {fmt_price(z["high"])}\n⚠️ شکست فیک ممکن است؛ دستور شورت نیست.'))
                except DataSourceError as e:
                    failed[symbol + ':1m'] = str(e)[:150]
            results.append(result)
        except DataSourceError as e:
            failed[symbol] = str(e)[:150]
        # Avoid rapid public API calls: no assumption about KCEX rate limits.
        if not demo:
            time.sleep(.2)
    results.sort(key=lambda x: (not x['near'], abs(x['dist_pct']), -x['growth1h']))
    report = {'created_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
              'source': 'KCEX futures (candidate endpoints; validation required)',
              'symbols_from_ticker': len(tickers), 'symbols_requested': len(wanted),
              'zones_active_seen': candidates, 'zones_near': sum(r['near'] for r in results),
              'data_errors': failed, 'results': results[:100]}
    save_json(REPORT_FILE, report)
    # Fail closed: if endpoint is systematically incompatible, do not pretend this worked.
    if len(failed) >= len(wanted):
        raise DataSourceError('All KCEX OHLC requests failed: KCEX futures candle path has not been verified. Check report.json for diagnostics. No alerts sent.')
    print(f'Scan: {len(tickers)} tickers, {len(wanted)} requested, {candidates} active zones, {len(failed)} data errors.')
    for row in results[:15]:
        print(f'{row["symbol"]:16s} {fmt_price(row["price"]):>12s}  zone {fmt_price(row["zone_low"])}..{fmt_price(row["zone_high"])}  d={row["dist_pct"]:+.1f}%  near={row["near"]}')
    for key, message in notifications:
        print('\n' + message + '\n')
        if demo or telegram_send(message):
            state['alerts'][key] = now
    # Keep state small; remove expired, old entries.
    state['alerts'] = {k: v for k, v in state['alerts'].items() if now - v < 48*3600}
    state['zones'] = {k: v for k, v in state['zones'].items() if v['expires_at'] >= now}
    if not demo:
        save_json(STATE_FILE, state)
    return report


class DemoKCEX:
    """Synthetic local dry-run; never presented as live KCEX."""
    def __init__(self, now):
        self.now = now

    def tickers(self):
        return [{'symbol': 'DEMOARIA_USDT', 'price': .0655, 'turnover': 900000,
                 'change24': 42.0, 'spread': .15}]

    def candles(self, symbol, interval, now):
        if interval == 'Min15':
            n = 155
            last_open = (now // 900 - 1) * 900
            out = []
            for i in range(n):
                t = last_open - (n-1-i)*900
                price = .044 + (.019 * i / (n - 1))
                if i == 50:  # confirmed historical peak
                    price = .067
                high = price + .0003
                if i == 50:
                    high = .0675
                v = 5000 if i < n-21 else (22000 if i == n-1 else 8000)
                out.append(Candle(t, price-.00015, high, price-.00035, price, v))
            # adjust last five bars for growth1h and last closed close
            out[-5:] = [Candle(b.t, .060 + j*.001, .0608 + j*.001, .0598 + j*.001,
                                  .060 + j*.001, b.v) for j,b in enumerate(out[-5:])]
            return out
        last_open = (now // 60 - 1) * 60
        return [Candle(last_open-(129-i)*60, .06, .0605, .0599, .0601, 2500) for i in range(130)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='offline demo with fake synthetic prices')
    args = parser.parse_args()
    now = int(time.time())
    provider = DemoKCEX(now) if args.demo else LiveKCEX()
    try:
        run(provider, now, demo=args.demo)
    except DataSourceError as e:
        print('DATA ERROR:', str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
