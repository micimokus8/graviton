import sys
sys.path.insert(0, '/root/.hermes/workspace/graviton')
import ccxt, numpy as np, datetime

ex = ccxt.krakenfutures({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
until = int(datetime.datetime(2026, 10, 26, 12, 46).timestamp() * 1000)

def ema(arr, n):
    a = 2.0 / (n + 1)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return np.array(out)

def tf_sig(rows):
    data = np.array([[float(x) for x in r[:6]] for r in rows])
    c = data[:, 4]
    e9, e20 = ema(c, 9)[-1], ema(c, 20)[-1]
    last3 = c[-3:]
    rising = all(last3[i] < last3[i+1] for i in range(2))
    falling = all(last3[i] > last3[i+1] for i in range(2))
    if c[-1] > e9 and e9 > e20: sig = 'BULL'
    elif e9 > e20 and rising: sig = 'BULL'
    elif c[-1] < e9 and e9 < e20: sig = 'BEAR'
    elif e9 < e20 and falling: sig = 'BEAR'
    else: sig = 'NEUTRAL'
    return sig, round(c[-1],2), round(e9,2), round(e20,2), 'rising' if rising else ('falling' if falling else 'flat')

print('=== QNT @ 12:46 UTC (Bias-Zeitpunkt) ===')
for tf in ('15m', '30m', '1h', '4h'):
    oh = ex.fetch_ohlcv('QNT/USD:USD', tf, limit=60, params={'until': until})
    sig, p, e9, e20, l3 = tf_sig(oh)
    print(f'{tf:4s}: {sig:7s} P={p} EMA9={e9} EMA20={e20} last3={l3}')

print()
print('=== 1H Close-Sequenz (letzte 8) ===')
oh = ex.fetch_ohlcv('QNT/USD:USD', '1h', limit=60, params={'until': until})
closes = [round(x[4],1) for x in oh[-8:]]
print(closes)
print('fallende Sequenz (letzte 4):', all(closes[i] > closes[i+1] for i in range(len(closes)-4, len(closes)-1)))

print()
print('=== BTC @ 12:46 UTC pro TF ===')
for tf in ('15m', '1h', '4h'):
    oh = ex.fetch_ohlcv('BTC/USD:USD', tf, limit=60, params={'until': until})
    sig, p, e9, e20, l3 = tf_sig(oh)
    print(f'BTC {tf:4s}: {sig:7s} P={p} last3={l3}')
