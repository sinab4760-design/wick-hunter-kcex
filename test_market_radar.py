"""Offline tests for experimental WICK HUNTER market radar. No network calls."""
import json
import tempfile
from pathlib import Path
import scanner_market_radar as r


def make_ticker(i, growth=0, price=1.0, hi=1.08, lo=.92):
    return {"symbol": f"TEST{i:03d}-USDT-SWAP", "price": price,
            "turnover_est_usdt": 700_000+i, "growth24h": growth,
            "high24h": hi, "low24h": lo, "spread_pct": .2}


def test_full_ticker_screen():
    now=1_000_020
    ts = [make_ticker(i, growth=10 + i / 5) for i in range(30)]
    ts += [make_ticker(i, growth=2, price=1.07) for i in range(30, 55)]
    ts += [make_ticker(i, growth=-5, price=.9) for i in range(55, 275)]
    selected, groups, counts = r.select_candidates(ts, {}, now, 40)
    assert counts['ticker_screened'] == 275
    assert len(selected) == 40
    assert any(v == 'PUMP' for v in groups.values())
    assert any(v == 'EARLY' for v in groups.values())
    assert len(groups) == len(set(groups))
    assert counts['pump_candidates'] == 30
    assert counts['early_candidates'] == 25
    tracked = {"TEST260-USDT-SWAP": {"expires": now+60}}
    selected2, groups2, _ = r.select_candidates(ts, tracked, now, 40)
    assert groups2['TEST260-USDT-SWAP'] == 'TRACKED'
    assert len(selected2) <= 40


def test_asset_filter():
    data={'code':'0','data':[
        {'instId':'AAA-USDT-SWAP','state':'live','instType':'SWAP','instCategory':'1'},
        {'instId':'SOXL-USDT-SWAP','state':'live','instType':'SWAP','instCategory':'3'},
        {'instId':'NO-USDT-SWAP','state':'live','instType':'SWAP'},
    ]}
    assert r.parse_instruments(data)=={'AAA-USDT-SWAP'}


def mins(now):
    end = now // 60 * 60
    t0 = end - 3600
    out = []
    for i in range(60):
        c = .059 + i * .000035
        low = c - .00025
        if i == 32:
            low = .058
        if i == 43:
            low = .059
        if i == 54:
            c, low = .059, .0588
        if i > 54:
            c = .059 - .00001 * (i - 54)
            low = c - .0002
        out.append(r.Candle(t0+i*60,c,max(c,low)+.00045,low,c,15000))
    return out


class Fake:
    def symbols(self, now):
        return [dict(make_ticker(0,growth=25,price=.0593,hi=.064,lo=.048),symbol='TEST-USDT-SWAP')]
    def candles(self,symbol,interval,now):
        if interval=='1m':
            return mins(now)
        t0 = now//900*900 - 120*900
        return [r.Candle(t0+i*900,.059,.060,.058,.059,20000) for i in range(120)]


def test_dual_alert_dedupe_and_dryrun():
    now=1_000_020
    bs=mins(now)
    z={'low': .0599, 'high':.063}
    watched=bs[50].t+60
    assert r.mini_break(bs[:53],z,watched,watched) is None
    event=r.mini_break(bs[:55],z,watched,watched)
    assert event and event['at']==bs[54].t+60
    with tempfile.TemporaryDirectory() as tmp:
        r.STATE_PATH=Path(tmp)/'state_radar.json'
        r.REPORT_PATH=Path(tmp)/'report_radar.json'
        old_state={'zones': {'TEST-USDT-SWAP': {
            'low':.0599, 'high':.063, 'source':'TEST',
            'created':bs[44].t+60, 'expires': now+1800}},'seen':{}}
        r.STATE_PATH.write_text(json.dumps(old_state))
        sent=[]
        r.send_telegram=lambda message: sent.append(message) or True
        a=r.run(Fake(),now)
        assert a['mini_breaks_detected_this_scan']==1
        assert a['selection']['ticker_screened']==1
        assert len(sent)==2, sent
        assert sent[0].startswith('🟡 WATCH') and sent[1].startswith('🔴 MINI BREAK')
        r.run(Fake(),now)
        assert len(sent)==2, 'Repeated Telegram notifications'
        r.run(Fake(),now,dry_run=True)
        assert len(sent)==2, 'Dry run must not send Telegram'
        assert len(r.load_state()['seen'])==2


if __name__=='__main__':
    test_full_ticker_screen()
    test_asset_filter()
    test_dual_alert_dedupe_and_dryrun()
    print('PASS: market-wide screen + tier selection, crypto category filter, ordered alerts, dedupe, dryrun')
