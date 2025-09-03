# YouTube Audio Converter

Ein schneller, robuster YouTube‑zu‑MP3 Downloader mit Streamlit-UI. Unterstützt Einzelvideos, öffentliche Playlists und YouTube Mix/Radio (bis zu 15 Songs). Inklusive automatischer MP3-Konvertierung (FFmpeg), ZIP-Erstellung für Mehrfachdownloads, Rate Limiting und sauberem Dateihandling.

- Einzelvideo: Direkt-Download als MP3 (Auto-Download via Browser)
- Playlist/Mix: Download aller Elemente als ZIP oder Auswahl einzelner Titel als ZIP
- Mix-Unterstützung: Extrahiert bis zu 15 Songs eines YouTube Radio/Mix
- Stabil: yt-dlp mit Format-Fallbacks, alternativen Player-Clients, Retries und Zeitlimits
- Ressourcen-/Sicherheitskontrollen: Rate-Limiting, aktive Downloadzähler, Größengrenzen, Garbage-Collection
- Saubere Dateinamen, konfigurierbare Grenzen, robuste ZIP-Erstellung

## Inhaltsverzeichnis

- Voraussetzungen
- Installation
- Starten der App
- Nutzung
  - Unterstützte URL-Typen
  - Einzelvideo-Download
  - Playlist-/Mix-Download (Komplett)
  - Playlist-/Mix-Download (Auswahl)
  - Lokale Videodatei zu MP3
- Konfiguration und Limits
- Architektur und wichtige Komponenten
- Fehlerbehebung (Troubleshooting)
- Sicherheit und rechtliche Hinweise
- FAQ
- Lizenz

## Voraussetzungen

- Python 3.10+ empfohlen
- FFmpeg im PATH (für die MP3-Konvertierung)
- Abhängigkeiten laut [`requirements.install()`](requirements.txt:1)

FFmpeg Installation:
- macOS (Homebrew): `brew install ffmpeg`
- Linux (Debian/Ubuntu): `sudo apt update && sudo apt install -y ffmpeg`
- Windows: Von https://ffmpeg.org/ laden und `ffmpeg.exe` in den PATH legen

## Installation

1) Projektquellcode bereitstellen (z. B. via Git Clone oder Download).
2) Virtuelle Umgebung anlegen und aktivieren:
- macOS/Linux:
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
- Windows (PowerShell):
  - `py -m venv .venv`
  - `.venv\Scripts\Activate.ps1`
3) Abhängigkeiten installieren:
- `pip install -r requirements.txt`

Hinweis: yt-dlp wird regelmäßig aktualisiert. Bei Problemen: `pip install --upgrade yt-dlp`.

## Starten der App

Standardstart (Port 8501, Host 0.0.0.0):
- `streamlit run main.py`


Die App startet Streamlit headless und bindet sich an Host/Port laut Parametern.
- Lokal erreichbar: http://localhost:8501 (oder gewählter Port)
- Netzwerkweit erreichbar (sofern Firewall erlaubt): http://0.0.0.0:8501

## Nutzung

1) URL-Feld: YouTube-URL einfügen (Video, Playlist oder Mix)
2) App erkennt automatisch den URL-Typ und bietet passende Optionen:
   - Einzelvideo: Download als MP3 mit Auto-Download im Browser
   - Playlist/Mix: 
     - Komplett als ZIP herunterladen
     - Einzelne Titel auswählen und ausgewählte als ZIP herunterladen

Unterstützte URL-Typen:
- ✅ Einzelne Videos
- ✅ Öffentliche Playlists
- ✅ YouTube Mix/Radio (RD-Playlists, bis zu 15 Songs)
- ⚠️ Liked Videos (LL): Nur Einzelvideo-Extraktion möglich
- ❌ Private Playlists, Upload-Listen (UL/UU), Watch Later (WL) sind nicht direkt downloadbar

### Einzelvideo-Download

- Füge eine YouTube-Video-URL ein, z. B.:
  - `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
- Die App:
  - Validiert die URL und ruft Videoinfos ab
  - Lädt das beste verfügbare Audio (Format-Fallbacks)
  - Konvertiert mit FFmpeg zu MP3 (320k CBR; Fallbacks vorhanden)
  - Startet den Auto-Download im Browser

Grenzen:
- Maximale Videodauer: 60 Minuten
- Max. MP3-Dateigröße: 100 MB

### Playlist-/Mix-Download (Komplett)

- Füge eine Playlist- oder Mix-URL ein, z. B.:
  - Playlist: `https://www.youtube.com/playlist?list=PL...`
  - Mix/Radio: URLs mit `list=RD...`
- Wähle „Komplette Playlist/Mix herunterladen“
- Die App lädt jedes Element sequenziell als MP3, erstellt eine ZIP:
  - ZIP-Dateiname = Titel der Playlist oder des Mixes
  - Auto-Download der ZIP bis 50 MB; darüber Download-Button

Grenzen:
- Playlists: bis zu 50 Videos
- Mix: bis zu 15 Songs
- ZIP-Autodownload: bis 50 MB

### Playlist-/Mix-Download (Auswahl)

- Füge eine Playlist- oder Mix-URL ein
- Wähle „Einzelne Songs auswählen“
- Markiere gewünschte Titel und starte den Download der Auswahl
- ZIP-Dateiname trägt den Playlist-/Mixtitel mit Suffix „(Auswahl)“

### Lokale Videodatei zu MP3

- Abschnitt „Lokale Videodatei in MP3 konvertieren“
- Lade Datei hoch (mp4, mkv, mov, webm, m4v, flv, avi)
- Die App konvertiert lokal via FFmpeg nach MP3 (320k) und bietet Download
- Eingabegröße begrenzt (Default: 100 MB)

## Konfiguration und Limits

Konstanten in [`python.main()`](main.py:21):
- MAX_DOWNLOADS_PER_IP = 10 pro Stunde
- MAX_DOWNLOADS_PER_SESSION = 500
- MAX_CONCURRENT_DOWNLOADS = 3
- MAX_FILE_SIZE_MB = 100
- MAX_VIDEO_DURATION = 3600 (Sekunden)
- MAX_PLAYLIST_SIZE = 50
- MAX_MIX_SIZE = 150 (Effektiv wird im UI Mix bis 15 Songs angekündigt; Code limitiert in Extraktion auf MAX_MIX_SIZE)
- RATE_LIMIT_SECONDS = 40
- MAX_ZIP_SIZE_MB = 50

Server-Defaults:
- DEFAULT_PORT = 8501
- DEFAULT_HOST = "0.0.0.0"

Weitere Einstellungen:
- yt-dlp nutzt Format-Fallbacks und alternative Player-Clients
- Postprocessing: MP3 mit FFmpeg (libmp3lame, 320k)
- Download erfolgt in temporären Verzeichnissen; Dateien werden nach Verarbeitung aufgeräumt

Anpassen:
- Werte oben in [`python.main()`](main.py:21) anpassen
- Beachte, dass sehr hohe Limits Serverressourcen und Browser-Handling beeinflussen können

## Architektur und wichtige Komponenten

- Streamlit App: UI, Status, Progress, Download-Trigger in [`python.main()`](main.py:1723)
- URL-Handling:
  - clean_youtube_url(url) in [`python.clean_youtube_url()`](main.py:887): Normalisiert und validiert YouTube-URLs, behandelt Spezialfälle (LL, RD)
  - is_valid_youtube_url(url) in [`python.is_valid_youtube_url()`](main.py:1610): Strikte Validierung
  - is_playlist_url(url) in [`python.is_playlist_url()`](main.py:949): Erkennung von Playlist-/Mix-URLs
  - handle_special_youtube_urls(url) in [`python.handle_special_youtube_urls()`](main.py:783): Liked Videos, Uploads, Watch Later, Mix
- Video-/Playlist-Info:
  - get_video_info(url) in [`python.get_video_info()`](main.py:1560)
  - extract_playlist_info(url) in [`python.extract_playlist_info()`](main.py:985) und Verarbeitung in [`python.process_playlist_entries()`](main.py:1102)
  - Mix-Extraktion: [`python.extract_mix_playlist_info()`](main.py:594), [`python.process_mix_entries()`](main.py:658), [`python.extract_mix_from_video_page()`](main.py:705)
- Download:
  - download_audio_with_progress(url, cb) in [`python.download_audio_with_progress()`](main.py:335): yt-dlp mit Format-Fallbacks und FFmpeg Postprocessing
  - download_multiple_videos(list, ...) in [`python.download_multiple_videos()`](main.py:1404): Sequenzieller Batch für Playlist/Mix
  - create_zip_file(items, name) in [`python.create_zip_file()`](main.py:1454) und Download-Links via [`python.create_zip_download_link()`](main.py:1497) bzw. Streamlit-Button [`python.create_streamlit_download_button()`](main.py:1545)
- Rate-Limiting und Ressourcen:
  - check_rate_limit(ip, session) in [`python.check_rate_limit()`](main.py:156)
  - update_download_tracking(...) in [`python.update_download_tracking()`](main.py:188)
  - check_system_resources() in [`python.check_system_resources()`](main.py:125)
  - cleanup_old_tracking_data() in [`python.cleanup_old_tracking_data()`](main.py:208)
- Dienstprogramme:
  - list_available_formats(url) in [`python.list_available_formats()`](main.py:310) (Debug)
  - clean_filename(name) in [`python.clean_filename()`](main.py:1665)
  - format_duration(sec) in [`python.format_duration()`](main.py:1597)

Serverstart:
- `run_server()` in [`python.run_server()`](main.py:2643) gibt Startmeldungen aus, Streamlit wird via `streamlit run` mit Parametern aus `__main__` gestartet.

## Fehlerbehebung (Troubleshooting)

Diagnose in der App:
- Button „Download-Diagnose“ zeigt Systemchecks an
- `diagnose_download_issues()` in [`python.diagnose_download_issues()`](main.py:276): 
  - prüft yt-dlp-Import/Version, FFmpeg-Verfügbarkeit, Temp-Verzeichnis-Schreibrechte
- Fehlerausgaben in der App geben Hinweise (z. B. „Video nicht verfügbar“, „zu lang“, „Timeout“)

Typische Probleme und Lösungen:
- yt-dlp nicht installiert/fehlerhaft:
  - `pip install --upgrade yt-dlp`
- FFmpeg nicht gefunden:
  - FFmpeg installieren und PATH prüfen
- „requested format is not available“:
  - Erneut versuchen; App nutzt Format-Fallbacks und alternative Clients
- Zeitüberschreitungen/Netzwerk:
  - Später erneut versuchen, stabile Verbindung sicherstellen
- Private/gesperrte Inhalte:
  - Nur öffentliche Inhalte sind unterstützt
- ZIP > 50 MB:
  - Automatischer Download via JS ist deaktiviert, stattdessen Download-Button nutzen

Logs:
- Konsole/Terminal zeigt zusätzliche Debug-Informationen (Formatliste, Fallbackpfade, Dateigrößen)

## Sicherheit und rechtliche Hinweise

- Verwenden Sie die App ausschließlich für Inhalte, deren Download und Nutzung Ihnen rechtlich gestattet ist.
- Beachten Sie YouTube-Nutzungsbedingungen und Urheberrechte.
- Rate-Limiting und Größenlimits helfen, Serverressourcen zu schützen und Missbrauch zu verhindern.

## FAQ

- Unterstützt die App auch 320k MP3?
  - Ja. Konvertierung mit libmp3lame, 320k CBR. Falls FFmpeg nötig ist, wird es verwendet.
- Kann ich mehrere Songs aus einer Playlist auswählen?
  - Ja, über den Auswahlmodus mit Checkboxen.
- Warum startet der ZIP-Download nicht automatisch?
  - Bei ZIPs > 50 MB ist Auto-Download deaktiviert. Nutzen Sie den bereitgestellten Download-Button.
- Warum sind einige spezielle Playlists nicht möglich?
  - Liked (LL), Upload (UL/UU) und Watch Later (WL) sind nicht öffentlich oder speziell behandelt. Die App konvertiert, wenn möglich, zur Einzelvideo-URL.

## Lizenz

Dieses Projekt wird „as is“ bereitgestellt. Prüfen Sie vor der Nutzung die rechtliche Lage in Ihrem Land und respektieren Sie Urheberrechte.

## Quickstart

- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt`
- `streamlit run main.py`
- Browser öffnen: http://localhost:8501
- YouTube-URL einfügen und Anweisungen in der App folgen