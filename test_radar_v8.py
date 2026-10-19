"""Wick Hunter V8 regression tests. Fully offline, no secrets/network."""
import json
import tempfile
from pathlib import Path
import scanner_market_radar_v8 as r

NOW = 1_000_020
SYMBOL = 'TEST-USDT-SWAP'
ZONE = {'low': .060, 'high': .062, 'source': 'TEST', 'created': NOW-300,
        'expires': NOW+3600}


def bars_1m(now=NOW, old_zone_wick=False, recent_zone_wick=False):
    latest_close = now // 60 * 60
    candles = []
    for i in range(70):
        at = latest_close - 70 * 60 + i * 60
        c = .0595
        high = .0596
        if old_zone_wick and i == 25:
            high = .061
        if recent_zone_wick and i == 68:
            high = .061
        candles.append(r.Candle(at, c, high, .0593, c, 3000))
    return candles


def bars_15m(now=NOW):
    end = now // 900 * 900
    out=[]
    for i in range(120):
        t = end - 120*900 + i*900
        out.append(r.Candle(t,.0595,.0597,.0592,.0595,30000))
    return out


class Fake:
    def __init__(self, minutes):
        self.minutes = minutes
    def symbols(self, now):
        return [{'symbol': SYMBOL, 'price': .0595, 'turnover_est_usdt': 5_000_000,
                 'growth24h': 8, 'spread_pct': .1,
                 'low24h': .054, 'high24h': .062}]
    def candles(self, sym, interval, now):
        return self.minutes if interval == '1m' else bars_15m(now)


def test_early_watch_and_dedupe():
    with tempfile.TemporaryDirectory() as d:
        r.STATE_PATH=Path(d)/'state_radar.json'
        r.REPORT_PATH=Path(d)/'report_radar.json'
        r.STATE_PATH.write_text(json.dumps({'zones':{SYMBOL:dict(ZONE)},'seen':{}}))
        messages=[]
        r.send_telegram=lambda text: messages.append(text) or True
        out=r.run(Fake(bars_1m()),NOW)
        assert out['new_early_watches_this_scan']==1
        assert len(messages)==1 and 'EARLY WATCH V8' in messages[0]
        assert 'candle closed' in messages[0]
        row=out['results'][0]
        assert row['watch_created_utc'] and row['watch_observed_utc']
        assert row['watch_data_lag_seconds']==0
        assert r.load_state()['zones'][SYMBOL]['watch_sent_at']==NOW
        r.run(Fake(bars_1m()),NOW)
        assert len(messages)==1, 'duplicated WATCH'


def test_past_wick_is_not_a_new_signal():
    for kind in ('old_zone_wick','recent_zone_wick'):
        with tempfile.TemporaryDirectory() as d:
            r.STATE_PATH=Path(d)/'state_radar.json'
            r.REPORT_PATH=Path(d)/'report_radar.json'
            r.STATE_PATH.write_text(json.dumps({'zones':{SYMBOL:dict(ZONE)},'seen':{}}))
            messages=[]
            r.send_telegram=lambda text: messages.append(text) or True
            out=r.run(Fake(bars_1m(**{kind:True})),NOW)
            assert out['new_early_watches_this_scan']==0
            assert out['already_wicked_watch_suppressed_this_scan']==1
            assert len(messages)==0, 'late alert was sent'
            assert r.load_state()['zones'][SYMBOL]['spent']


def test_old_data_never_alerts():
    fresh = bars_1m()
    gate=r.first_pre_wick_watch(fresh,ZONE,NOW+400,.0595)
    assert gate['reason']=='STALE_1M_CANDLE' and not gate['eligible']
    assert r.first_pre_wick_watch(fresh,ZONE,NOW,.0595)['eligible']
    # Price already inside resistance is not a pre-wick warning.
    assert r.first_pre_wick_watch(fresh, ZONE, NOW, .0603)['eligible'] is False


def test_post_watch_touch_and_break_lag():
    # A new MINI BREAK requires a post-WATCH visit to resistance vicinity.
    fresh=bars_1m()
    watch=fresh[45].t+60
    assert r.post_watch_touch_at(fresh,ZONE,watch)==0
    b2=bars_1m(recent_zone_wick=True)
    assert r.post_watch_touch_at(b2,ZONE,watch)==b2[68].t+60
    assert r.MAX_BREAK_ALERT_LAG_SEC < 300


def test_dry_run_does_not_send_or_persist():
    with tempfile.TemporaryDirectory() as d:
        r.STATE_PATH=Path(d)/'state_radar.json'
        r.REPORT_PATH=Path(d)/'report_radar.json'
        messages=[]
        r.send_telegram=lambda text: messages.append(text) or True
        report=r.run(Fake(bars_1m()),NOW,dry_run=True)
        assert isinstance(report,dict)
        assert not r.STATE_PATH.exists() and not messages


def break_minutes():
    base = bars_1m()
    candles=[]
    for i, bar in enumerate(base[-60:]):
        c=.059+i*.000035
        low=c-.00025
        if i==32:
            low=.058
        if i==43:
            low=.059
        if i==54:
            c,low=.059,.0588
        if i>54:
            c=.059-.00001*(i-54)
            low=c-.0002
        candles.append(r.Candle(bar.t,c,max(c,low)+.00045,low,c,15000))
    return candles


def test_break_requires_touch_and_stale_break_is_suppressed():
    bs = break_minutes()
    watch_at = bs[30].t+60
    first_touch=r.post_watch_touch_at(bs[:50],{'low':.0599,'high':.063},watch_at)
    assert first_touch > watch_at
    event = r.mini_break(bs, {'low':.0599,'high':.063}, first_touch, 0)
    assert event and event['at'] == bs[54].t + 60
    for timely in (True,False):
        with tempfile.TemporaryDirectory() as d:
            r.STATE_PATH=Path(d)/'state_radar.json'
            r.REPORT_PATH=Path(d)/'report_radar.json'
            zone={**ZONE,'low':.0599,'high':.063, 'created':bs[10].t,
                  'expires':bs[59].t+9999,'watch_at':watch_at,
                  'watch_version':8}
            r.STATE_PATH.write_text(json.dumps({'zones':{SYMBOL:zone},'seen':{}}))
            messages=[]
            r.send_telegram=lambda msg: messages.append(msg) or True
            class Source(Fake):
                def symbols(self,now):
                    return [{'symbol':SYMBOL,'price': .0593,'turnover_est_usdt':5_000_000,
                             'growth24h':8,'spread_pct': .1,
                             'low24h':.054,'high24h':.065}]
            now = bs[55].t+60 if timely else bs[59].t+60
            report=r.run(Source(bs[:56] if timely else bs), now)
            assert report['mini_breaks_detected_this_scan']==1
            if timely:
                assert len(messages)==1 and 'MINI BREAK V8' in messages[0]
                assert r.load_state()['zones'][SYMBOL]['break_sent_at']==now
            else:
                assert not messages, 'An out-of-date mini-break was sent'
                assert report['late_break_alerts_suppressed_this_scan']==1


def test_asset_filter():
    data={'code':'0','data':[
        {'instId':'AAA-USDT-SWAP','state':'live','instType':'SWAP','instCategory':'1'},
        {'instId':'SOXL-USDT-SWAP','state':'live','instType':'SWAP','instCategory':'3'},
    ]}
    assert r.parse_instruments(data)=={'AAA-USDT-SWAP'}

if __name__ == '__main__':
    test_early_watch_and_dedupe()
    test_past_wick_is_not_a_new_signal()
    test_old_data_never_alerts()
    test_post_watch_touch_and_break_lag()
    test_dry_run_does_not_send_or_persist()
    test_break_requires_touch_and_stale_break_is_suppressed()
    test_asset_filter()
    print('PASS: 7 V8 tests / pre-wick WATCH, dedupe, prior-wick and late-break suppression, stale-data, touch, dry-run, filter')
