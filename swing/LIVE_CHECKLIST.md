# Graviton Swing — Checkliste vor dem Livegang

Stand 13.11.2026. Das Swing-System läuft **DRY_RUN** (Papierbuch
`data/swing_state.json`). Diese Punkte müssen erledigt sein, bevor echtes Geld
fließt. Reihenfolge = Priorität.

## 1. Echte Stop-Order an der Börse (kritisch)
Aktuell sind Stop und Ziel **Buchhaltungs-Marken**; geprüft wird alle 30 Min.
Zwischen zwei Prüfungen kann der Kurs den Stop überschießen — gebucht wird
trotzdem zum Stop-Preis, das ist optimistisch.
→ Beim Livegang die Stop-Order **direkt bei Kraken platzieren**
(`ExecutionAdapter` existiert noch im alten Code). Der Exit-Wächter bleibt als
zweite Linie für TP1/Trendbruch.

## 2. Kombinierte Exit-Regel backtesten (offen)
Der Backtest lief mit „Trendbruch-Exit, kein TP". Die jetzige Regel
(Widerstandsziel + 50 % Teilverkauf + Breakeven-Stop) ist **nur unit-getestet**.
Vor Live: Erwartungswert dieser Kombi messen, sonst kennen wir die Zahl nicht.

## 3. Positionsgröße und Kontrakt-Mindestgrößen
- `EQUITY_USD` wird ignoriert, Konto ist in EUR bei Kraken (~493 €).
- `min_contract` je Symbol prüfen (Kraken lehnt zu kleine Orders ab —
  im alten Log gab es „0.0035 < min 0.01 → Abbruch").
- Feste Positionsgröße definieren, nicht pro Trade improvisieren.

## 4. Kosten und Slippage verifizieren
Backtest rechnet 10 bps pro Fill (= 20 bps Round-Trip). Live: echte Kraken-Gebühren
+ Spread der Aktien-Perps (dünn außerhalb US-Hours) gegenrechnen.

## 5. Gap-Risiko über Nacht / Wochenende
Perps handeln durch, die Underlyings nicht. Wochenend-Gaps sind real.
`FRIDAY_MODE = "protect"` schließt nur Verlierer — Wirkung im Paper prüfen.

## 6. Alarm- und Ausfallpfad
- Was passiert, wenn der Cron ausfällt (Position unbeaufsichtigt)?
- Was, wenn Telegram nicht zustellt?
- Was, wenn Kraken die Order ablehnt?
→ Mindestens ein Wachhund, der „Position offen, aber kein Lauf seit X" meldet.

## 7. Kill-Switch dokumentieren
Wie stoppe ich alles sofort? Jobs pausieren **und** Position schließen.
Muss ohne Nachdenken gehen.

## 8. Paper-Ergebnis gegen Erwartung prüfen
2–4 Wochen laufen lassen. Erwartung aus der Validierung: Ø +1,8 % pro Trade,
Ø 10 Tage Haltedauer, ~19 Trades/Jahr. Weicht das Paper stark ab → Ursache
suchen, **bevor** live geschaltet wird.

## 9. Parameter mit Setzungs-Charakter bewusst bestätigen
Diese Werte sind gesetzt, nicht gemessen:
- `NOT_AT_TOP_ATR = 0.5` (Mindest-Pullback, sonst kein Kauf am Hoch / Short am Tief)
- `MIN_STOP_ATR = 0.8`, `STOP_BUFFER_ATR = 0.25`, `RISK_ATR_CAP = 1.5`
- `TARGET_MIN/MAX_ATR = 1.5 / 4.0`, gemessenes Ziel 2,5 ATR
- Pivots: `k = 5`, letzte 60 Handelstage, mindestens 0,4 ATR groß
- `MAX_HOLD_BARS = 20` (Tageskerzen, nicht Kalendertage)
- `ALLOW_LONG` / `ALLOW_SHORT` in `swing/config.py`
- `FRIDAY_MODE = "protect"`

## 10. Erst dann umschalten
Alte Daytrading-Jobs löschen, neuen Jobs vertrauen, Live-Größe klein starten
(nicht die Papiermenge 1:1).
