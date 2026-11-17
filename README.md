# Teufel Raumfeld für Home Assistant

Custom Integration, die Teufel-Raumfeld-Lautsprecher (z. B. **Teufel One S**) als
Media-Player in Home Assistant einbindet. Die Steuerung läuft rein lokal über den
Raumfeld-Host-Webservice (Port 47365) und UPnP – ohne Cloud, ohne zusätzliche
Python-Pakete.

## Funktionen

**Media-Player** (einer pro Raumfeld-Raum, wird automatisch angelegt)

- Wiedergabe, Pause, Stopp, nächster/vorheriger Titel, Spulen
- Lautstärke und Stummschaltung
- Titel, Interpret, Album, Sender, Cover, Laufzeit und Position
- Zufallswiedergabe und Wiederholung
- Ein-/Ausschalten (Standby)
- **Quellen:** die Favoriten aus der Teufel-App und der Line-In-Eingang
- **Medienbrowser:** die Raumfeld-Bibliothek (Favoriten, TuneIn, Playlists, …) und die
  Medienquellen von Home Assistant (lokale Medien, Radio Browser)
- Abspielen beliebiger Audio-URLs und Sprachausgabe (TTS)
- **Durchsagen:** Mit `announce: true` läuft danach weiter, was vorher lief
- **Multiroom:** Räume gruppieren und trennen (`media_player.join` / `unjoin`)

**Weitere Entitäten je Raum**

- Zahlenfelder *Bass*, *Mitten*, *Höhen* (±6 dB in 0,6-dB-Stufen)
- Zahlenfeld *Sleep-Timer* in Minuten (0 = aus)
- Tasten *Hinweiston Erfolg* und *Hinweiston Fehler* (unterbrechen die Wiedergabe nicht)
- Sensor *WLAN-Signal*
- *Firmware*: zeigt installierte und verfügbare Version (Einspielen bleibt bei der Teufel-App)

Änderungen an der Box oder in der Teufel-App erscheinen sofort, weil die Box sie per
UPnP-Ereignis meldet. Kommen keine Ereignisse an, fragt die Integration alle 5 Sekunden ab.

### Beispiele

```yaml
# Favoriten starten
action: media_player.select_source
target:
  entity_id: media_player.wohnzimmer
data:
  source: SWR3
```

```yaml
# Durchsage, danach läuft der Sender weiter
action: tts.speak
target:
  entity_id: tts.home_assistant_cloud   # eigene TTS-Entität eintragen
data:
  media_player_entity_id: media_player.wohnzimmer
  message: Es hat geklingelt.
  options: {}
```

`tts.speak` und Assist-Durchsagen setzen `announce` von selbst; bei
`media_player.play_media` gibt man `announce: true` an.

## Installation

### Manuell

1. Den Ordner `custom_components/teufel_raumfeld` nach
   `<config>/custom_components/teufel_raumfeld` auf dem Home-Assistant-Rechner kopieren
   (z. B. über das Samba- oder das „Studio Code Server"-Add-on).
2. Home Assistant neu starten.

### Über HACS

1. In HACS oben rechts *⋮ → Benutzerdefinierte Repositories* öffnen.
2. `https://github.com/mztechnik/hacs-teufel-raumfeld` mit dem Typ *Integration* eintragen.
3. „Teufel Raumfeld" installieren und Home Assistant neu starten.

## Einrichtung

Nach dem Neustart erscheint die Box unter *Einstellungen → Geräte & Dienste* in der
Regel von selbst als „Entdeckt". Andernfalls *Integration hinzufügen → Teufel Raumfeld*
wählen und die IP-Adresse der Box eintragen. Der Box sollte im Router eine feste
IP-Adresse zugewiesen sein.

Voraussetzung: Home Assistant (min. Version 2025.3) und die Box liegen im selben Netz.

## Hinweise

- **Kein Media-Player nach der Einrichtung?** Dann meldet die Box gerade keinen Raum
  (`http://<IP-der-Box>:47365/getZones` ist leer). Box kurz vom Strom nehmen und in der
  Teufel-Raumfeld-App prüfen, ob sie dort abspielbar ist. Sobald die Box den Raum wieder
  meldet, legt die Integration den Media-Player ohne Neustart an.
- **Sprachausgabe/lokale Medien:** Die Box lädt die Datei direkt von Home Assistant.
  Unter *Einstellungen → System → Netzwerk* muss dafür eine interne URL mit `http://`
  eingetragen sein, die die Box erreichen kann.
- **Sofort-Aktualisierung:** Die Box schickt ihre Meldungen an einen freien Port von
  Home Assistant. Läuft Home Assistant in einem Container ohne Host-Netzwerk oder hinter
  einer Firewall, kommen sie nicht an – dann bleibt es bei der Abfrage alle 5 Sekunden.
- **Quellenliste:** Neue Favoriten aus der Teufel-App erscheinen nach spätestens 2 Minuten.
- **Durchsagen:** Lief vorher nichts, bleibt die Box nach der Durchsage still; war sie im
  Standby, geht sie danach wieder in den Standby.
- **Nicht unterstützt** von der One S: Balance und Stereo-Verbreiterung.

## Fehlersuche

```yaml
logger:
  logs:
    custom_components.teufel_raumfeld: debug
```
