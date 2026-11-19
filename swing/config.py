"""Schalter für den Swing-Scanner.

Papierhandel only — hier gibt es keine Orders und keine API-Keys.
Beide Seiten an: der stärkste Kandidat (Long oder Short) wird genommen.
Eine Seite auf False setzen, dann wird sie ignoriert. Der nächste
Tageslauf liest die Datei neu (Cron startet jedes Mal frisch).
"""

# True = diese Seite darf als Papier-Trade vorgeschlagen werden.
ALLOW_LONG = True
ALLOW_SHORT = True
