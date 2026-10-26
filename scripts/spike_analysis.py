import sys, json
sys.path.insert(0, '/root/.hermes/workspace/graviton')
import ccxt, numpy as np, datetime

ex = ccxt.krakenfutures({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
rows = [json.loads(l) for l in open('/root/.hermes/workspace/graviton/data/trade_log.jsonl')]
ents = [r for r in rows if r.get('event') == 'entry']
exits = [r for r in rows if r.get('event') == 'exit']

# Entry-Exit-Matching: Exit-Events NACH dem Entry, gleiche Richtung, bis zum nächsten Entry des Symbols
ents2 = []
for e in ents:
    if e['timestamp'][:4] != '2026' or e['timestamp'][:10] < '2026-09-15':
        continue
    outs = [x for x in exits if x.get('base') == e.get('base')
            and x['timestamp'] > e['timestamp']
            and x.get('bias') == e.get('bias')
            and not any(o['timestamp'] > e['timestamp'] and o['timestamp'] < x['timestamp']
                        and o.get('bias') == e.get('bias') for o in exits if o.get('base') == e.get('base'))]
    if outs:
        ents2.append((e, outs))

spikes = []
for e, outs in ents2:
    ts = e.get('timestamp', '')
    t0 = datetime.datetime.fromisoformat(ts[:19] + '+00:00')
    sym = e.get('symbol')
    bias = e.get('bias')
    try:
        oh = ex.fetch_ohlcv(sym, '1h', limit=1500)
        hist = [r for r in oh if r[0] + 3600000 <= t0.timestamp() * 1000]
        if len(hist) < 12:
            continue
        c = [r[4] for r in hist]
        spike = (c[-1] - c[-3]) / c[-3] * 100  # 2h-Change vor Entry (Bias lag ~10min davor)
        e20 = float(np.mean(c[-20:]))
        dist = (c[-1] - e20) / e20 * 100
        pnl = sum(x.get('pnl_pct', 0) for x in outs)
        spikes.append((ts[:10], e.get('base'), bias, spike, dist, pnl))
        print(f"{ts[5:16]:11s} {e.get('base'):9s} {bias:5s} {spike:+8.1f}% {dist:+10.1f}% {pnl:+6.2f}%")
    except Exception as err:
        print(ts[:16], e.get('base'), 'ERR', str(err)[:60])

print()
# Spike-Buckets
for lo, hi, label in ((-99, 2, 'flach (<+2%)'), (2, 6, 'moderat (+2-6%)'), (6, 99, 'SPIKE (>+6%)')):
    sel = [(s, d, p) for _, _, b, s, d, p in spikes if b == 'LONG' and lo <= s < hi]
    if sel:
        pnls = [p for _, _, p in sel if p is not None]
        wr = 100 * sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0
        print(f"LONG {label:16s}: n={len(sel):2d} PnLs={pnls} WR={wr:.0f}%")
