"""Konstanten der Teufel-Raumfeld-Integration."""

from datetime import timedelta

DOMAIN = "teufel_raumfeld"
MANUFACTURER = "Lautsprecher Teufel"

# Abfrage-Intervall ohne bzw. mit funktionierendem Ereignis-Empfang.
SCAN_INTERVAL = timedelta(seconds=5)
SCAN_INTERVAL_WITH_EVENTS = timedelta(seconds=30)

# Selten wechselnde Daten (Favoriten, WLAN-Signal, Firmware) in Sekunden.
SLOW_INTERVAL = 120

# Wartezeit, bis eine neu angelegte Zone samt virtuellem Renderer bereitsteht.
ZONE_WAIT_ATTEMPTS = 20
ZONE_WAIT_DELAY = 0.5

# Durchsagen: Wartezeit auf den Start und maximale Dauer in Sekunden.
ANNOUNCE_START_TIMEOUT = 15
ANNOUNCE_MAX_DURATION = 600

MEDIA_ID_PREFIX = "raumfeld://"
SOURCE_LINE_IN = "Line-In"
