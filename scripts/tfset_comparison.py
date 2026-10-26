import sys, json
sys.path.insert(0, '/root/.hermes/workspace/graviton')
import ccxt, numpy as np, datetime

ex = ccxt.krakenfutures({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
TFS = {'15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000}

def ema(arr, n):
    a = 2 / (n + 1)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return np.array(out)

def tf_sig(rows, tf, cut):
    tfms = TFS[tf]
    sel = [r for r in rows if r[0] + tfms <= cut]
    if not sel:
        return None
    data = np.array([[float(x) for x in r[:6]] for r in sel])
    c = data[:, 4]
    if len(c) < 25:
        return None
    e9, e20 = ema(c, 9)[-1], ema(c, 20)[-1]
    l3 = c[-3:]
    rising = all(l3[i] < l3[i+1] for i in range(2))
    falling = all(l3[i] > l3[i+1] for i in range(2))
    if c[-1] > e9 and e9 > e20: s = 'BULL'
    elif e9 > e20 and rising: s = 'BULL'
    elif c[-1] < e9 and e9 < e20: s = 'BEAR'
    elif e9 < e20 and falling: s = 'BEAR'
    else: s = 'NEUTRAL'
    return s

rows = [json.loads(l) for l in open('/root/.hermes/workspace/graviton/data/trade_log.jsonl')]
ents = [r for r in rows if r.get('event') == 'entry' and r['timestamp'][:10] >= '2026-09-15']
exits = [r for r in rows if r.get('event') == 'exit']

flip = {'besser': 0, 'schlechter': 0, 'gleich': 0, 'kein_trade_mehr': 0}
details = []
cache = {}
for e in ents:
    t0 = datetime.datetime.fromisoformat(e['timestamp'][:19] + '+00:00')
    cut = int((t0 - datetime.timedelta(minutes=9)).timestamp() * 1000)  # Bias-Zeitpunkt
    sym = e.get('symbol')
    if sym not in cache:
        cache[sym] = {tf: ex.fetch_ohlcv(sym, tf, limit=800) for tf in TFS}
    sigs_alt = [tf_sig(cache[sym][tf], tf, cut) for tf in ('15m', '1h', '4h')]
    sigs_neu = [tf_sig(cache[sym][tf], tf, cut) for tf in ('15m', '30m', '1h')]
    def count3(sig):
        b = sum(1 for x in sig if x == 'BULL')
        s = sum(1 for x in sig if x == 'BEAR')
        return (b, s)
    b_alt, s_alt = count3(sigs_alt)
    b_neu, s_neu = count3(sigs_neu)
    bias = e.get('bias')
    # Bias-Konsistenz alt/neu
    def consistent(sig, bias):
        want = 'BULL' if bias == 'LONG' else 'BEAR'
        return sig.count(want) == 3
    alt_ok = consistent(sigs_alt, bias)
    neu_ok = consistent(sigs_neu, bias)
    pnl = None
    outs = [x for x in exits if x.get('base') == e.get('base') and x['timestamp'] > e['timestamp']
            and x.get('bias') == bias]
    if outs:
        pnl = sum(x.get('pnl_pct', 0) for x in outs)
    tag = ''
    if alt_ok and not neu_ok: tag = 'NEU-SET haette BLOCKIERT'
    elif not alt_ok and neu_ok: tag = 'NEU-SET haette NICHT blockiert'
    elif not alt_ok and not neu_ok: tag = 'beide blockieren'
    else: tag = 'gleich (3/3 beide)'
    sa = ','.join(x or '?' for x in sigs_alt)
    sn = ','.join(x or '?' for x in sigs_neu)
    details.append((e['timestamp'][:10], e.get('base'), bias, pnl, tag, sa, sn))
    print(f"{e['timestamp'][5:16]} {e.get('base'):9s} {bias:5s} "
          f"{'--' if pnl is None else format(pnl, '+6.2f')}  ALT[{sa}] NEU[{sn}]  {tag}")

print()
from collections import Counter
c = Counter(d[4] for d in details)
for k, v in c.items():
    print(f'{k}: {v}')
