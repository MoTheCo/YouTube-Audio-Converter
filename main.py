import streamlit as st
import yt_dlp
import os
import tempfile
from pathlib import Path
import time
import re
import threading
from queue import Queue
from urllib.parse import urlparse, parse_qs
import base64
import hashlib
from datetime import datetime, timedelta
import resource
import gc
import sys
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== SICHERHEITSKONFIGURATION =====
MAX_DOWNLOADS_PER_IP = 10  # Max Downloads pro IP pro Stunde
MAX_DOWNLOADS_PER_SESSION = 500  # Max Downloads pro Session
MAX_CONCURRENT_DOWNLOADS = 3  # Max gleichzeitige Downloads
MAX_FILE_SIZE_MB = 100  # Max Dateigröße in MB
MAX_VIDEO_DURATION = 3600  # Max Video-Länge in Sekunden (1 Stunde)
MAX_PLAYLIST_SIZE = 50  # Max Playlist-Größe
MAX_MIX_SIZE = 150  # Max Mix-Größe
RATE_LIMIT_SECONDS = 40 # Mindestabstand zwischen Downloads
MAX_ZIP_SIZE_MB = 50  # Max ZIP-Größe für automatischen Download

# SERVER KONFIGURATION
DEFAULT_PORT = 8501
DEFAULT_HOST = "0.0.0.0"

# Globale Variablen für Rate Limiting
if 'download_queue' not in st.session_state:
    st.session_state.download_queue = {}
if 'ip_downloads' not in st.session_state:
    st.session_state.ip_downloads = {}
if 'active_downloads' not in st.session_state:
    st.session_state.active_downloads = 0
if 'last_download_time' not in st.session_state:
    st.session_state.last_download_time = {}
if 'playlist_videos' not in st.session_state:
    st.session_state.playlist_videos = []
if 'selected_videos' not in st.session_state:
    st.session_state.selected_videos = []
if 'batch_download_in_progress' not in st.session_state:
    st.session_state.batch_download_in_progress = False

# ===== WERBEPLATZHALTER (VERSTECKT) =====
AD_SLOT_HEADER = """
<!-- 
<div id="ad-slot-header" style="display: none;">
    <div class="ad-banner-top">
        <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
        <ins class="adsbygoogle"
             style="display:block"
             data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
             data-ad-slot="XXXXXXXXXX"
             data-ad-format="auto"></ins>
        <script>
             (adsbygoogle = window.adsbygoogle || []).push({});
        </script>
    </div>
</div>
-->
"""

AD_SLOT_SIDEBAR = """
<!-- 
<div id="ad-slot-sidebar" style="display: none;">
    <div class="ad-sidebar">
        <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
        <ins class="adsbygoogle"
             style="display:block"
             data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
             data-ad-slot="XXXXXXXXXX"
             data-ad-format="rectangle"></ins>
        <script>
             (adsbygoogle = window.adsbygoogle || []).push({});
        </script>
    </div>
</div>
-->
"""

AD_SLOT_FOOTER = """
<!-- 
<div id="ad-slot-footer" style="display: none;">
    <div class="ad-banner-bottom">
        <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
        <ins class="adsbygoogle"
             style="display:block"
             data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
             data-ad-slot="XXXXXXXXXX"
             data-ad-format="banner"></ins>
        <script>
             (adsbygoogle = window.adsbygoogle || []).push({});
        </script>
    </div>
</div>
-->
"""

def get_client_ip():
    """Hole Client IP für Rate Limiting"""
    try:
        # Streamlit Cloud/Deployed Version
        if hasattr(st, 'query_params'):
            headers = st.context.headers if hasattr(st.context, 'headers') else {}
            return headers.get('x-forwarded-for', 'unknown').split(',')[0].strip()
        return 'localhost'
    except:
        return 'unknown'

def get_session_id():
    """Erstelle Session ID"""
    try:
        return st.session_state.get('session_id', hashlib.md5(str(time.time()).encode()).hexdigest())
    except:
        return 'default'

def check_system_resources():
    """Überprüfe Systemressourcen ohne psutil"""
    try:
        # Memory Check mit resource module (nur Linux/Unix)
        try:
            import resource
            max_memory = resource.getrlimit(resource.RLIMIT_AS)[0]
            if max_memory != resource.RLIM_INFINITY:
                current_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # Konvertierung je nach System (Linux: KB, macOS: Bytes)
                if current_memory > 1000000:  # Wahrscheinlich Bytes
                    current_memory_mb = current_memory / (1024 * 1024)
                else:  # Wahrscheinlich KB
                    current_memory_mb = current_memory / 1024
                
                if current_memory_mb > 500:  # 500MB Limit
                    return False, f"Speicher-Limit erreicht ({current_memory_mb:.1f}MB)"
        except:
            pass
        
        # Einfacher Load Check über aktive Downloads
        if st.session_state.active_downloads >= MAX_CONCURRENT_DOWNLOADS:
            return False, "Server überlastet - zu viele aktive Downloads"
        
        # Garbage Collection für Speicherfreigabe
        gc.collect()
        
        return True, "OK"
    except Exception as e:
        return True, "OK"  # Fallback

def check_rate_limit(client_ip, session_id):
    """Überprüfe Rate Limiting"""
    current_time = datetime.now()
    
    # Session Rate Limit
    session_downloads = st.session_state.get('session_download_count', 0)
    if session_downloads >= MAX_DOWNLOADS_PER_SESSION:
        return False, f"Session-Limit erreicht ({MAX_DOWNLOADS_PER_SESSION} Downloads)"
    
    # IP Rate Limit (pro Stunde)
    if client_ip in st.session_state.ip_downloads:
        ip_downloads = st.session_state.ip_downloads[client_ip]
        # Entferne Downloads älter als 1 Stunde
        recent_downloads = [t for t in ip_downloads if current_time - t < timedelta(hours=1)]
        st.session_state.ip_downloads[client_ip] = recent_downloads
        
        if len(recent_downloads) >= MAX_DOWNLOADS_PER_IP:
            return False, f"IP-Limit erreicht ({MAX_DOWNLOADS_PER_IP} Downloads/Stunde)"
    
    # Zeit zwischen Downloads
    if session_id in st.session_state.last_download_time:
        time_since_last = (current_time - st.session_state.last_download_time[session_id]).total_seconds()
        if time_since_last < RATE_LIMIT_SECONDS:
            remaining = RATE_LIMIT_SECONDS - int(time_since_last)
            return False, f"Bitte warten Sie {remaining} Sekunden"
    
    # Concurrent Downloads
    if st.session_state.active_downloads >= MAX_CONCURRENT_DOWNLOADS:
        return False, "Zu viele gleichzeitige Downloads. Bitte warten Sie."
    
    return True, "OK"

def update_download_tracking(client_ip, session_id):
    """Aktualisiere Download-Tracking"""
    current_time = datetime.now()
    
    # Session Counter
    if 'session_download_count' not in st.session_state:
        st.session_state.session_download_count = 0
    st.session_state.session_download_count += 1
    
    # IP Tracking
    if client_ip not in st.session_state.ip_downloads:
        st.session_state.ip_downloads[client_ip] = []
    st.session_state.ip_downloads[client_ip].append(current_time)
    
    # Last Download Time
    st.session_state.last_download_time[session_id] = current_time
    
    # Active Downloads Counter
    st.session_state.active_downloads += 1

def cleanup_old_tracking_data():
    """Bereinige alte Tracking-Daten"""
    try:
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=2)
        
        # Bereinige IP Downloads
        for ip in list(st.session_state.ip_downloads.keys()):
            st.session_state.ip_downloads[ip] = [
                t for t in st.session_state.ip_downloads[ip] 
                if t > cutoff_time
            ]
            # Entferne leere Einträge
            if not st.session_state.ip_downloads[ip]:
                del st.session_state.ip_downloads[ip]
        
        # Bereinige Last Download Times
        for session_id in list(st.session_state.last_download_time.keys()):
            if st.session_state.last_download_time[session_id] < cutoff_time:
                del st.session_state.last_download_time[session_id]
    except:
        pass

def test_yt_dlp_installation():
    """Teste ob yt-dlp korrekt installiert ist"""
    try:
        import yt_dlp
        print(f"yt-dlp Version: {yt_dlp.version.__version__}")
        
        # Teste mit einer einfachen URL
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Rick Roll als Test
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 15,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
            if info:
                print("yt-dlp Installation erfolgreich getestet")
                return True, "OK"
            else:
                return False, "Keine Info erhalten"
                
    except ImportError:
        return False, "yt-dlp nicht installiert"
    except Exception as e:
        return False, f"yt-dlp Test fehlgeschlagen: {str(e)}"

def check_ffmpeg_installation():
    """Prüfe ob FFmpeg verfügbar ist"""
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print("FFmpeg ist verfügbar")
            return True, "OK"
        else:
            return False, "FFmpeg nicht gefunden"
    except FileNotFoundError:
        return False, "FFmpeg nicht installiert"
    except Exception as e:
        return False, f"FFmpeg Test fehlgeschlagen: {str(e)}"

def diagnose_download_issues():
    """Diagnose möglicher Download-Probleme"""
    issues = []
    
    # Teste yt-dlp
    yt_dlp_ok, yt_dlp_msg = test_yt_dlp_installation()
    if not yt_dlp_ok:
        issues.append(f"yt-dlp Problem: {yt_dlp_msg}")
    
    # Teste FFmpeg
    ffmpeg_ok, ffmpeg_msg = check_ffmpeg_installation()
    if not ffmpeg_ok:
        issues.append(f"FFmpeg Problem: {ffmpeg_msg}")
    
    # Teste Temp-Verzeichnis
    try:
        temp_dir = tempfile.mkdtemp()
        test_file = os.path.join(temp_dir, "test.txt")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        os.rmdir(temp_dir)
    except Exception as e:
        issues.append(f"Temp-Verzeichnis Problem: {str(e)}")
    
    return issues

def get_best_format_options():
    """Get list of audio-first format options to try (MP3-Ausgabe)"""
    # Wir wählen bestaudio (quelle egal), konvertieren aber stets zu MP3 via Postprocessor
    return [
        'bestaudio/best'
    ]

def list_available_formats(url):
    """List available formats for debugging (zusätzliche Details)"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            print(f"Verfügbare Formate für {info.get('title', 'Unknown')} ({len(formats)}):")
            for fmt in formats:
                fid = fmt.get('format_id', 'N/A')
                ext = fmt.get('ext', 'N/A')
                acodec = fmt.get('acodec', 'N/A')
                vcodec = fmt.get('vcodec', 'N/A')
                abr = fmt.get('abr', 'N/A')
                asr = fmt.get('asr', 'N/A')
                note = fmt.get('format_note', '')
                print(f"  id={fid} ext={ext} acodec={acodec} vcodec={vcodec} abr={abr} asr={asr} note={note}")
            return formats
    except Exception as e:
        print(f"Fehler beim Auflisten der Formate: {str(e)}")
        return []

def download_audio_with_progress(url, progress_callback=None):
    """Download nur-Audio als MP3 mit robustem Fallback und klarer Formatwahl"""
    try:
        temp_dir = tempfile.mkdtemp()
        download_start_time = time.time()
        print(f"Starte Download für: {url}")
        print(f"Temp-Verzeichnis: {temp_dir}")
        # Zusätzliche Debug-Ausgabe: verfügbare Formate listen (hilft bei "Requested format is not available")
        try:
            print("Debug: Liste verfügbare Formate...")
            list_available_formats(url)
        except Exception as _e:
            print(f"Formate-Listing übersprungen: {_e}")

        def progress_hook(d):
            if progress_callback and d.get('status') == 'downloading':
                # Timeout-Check (max 5 Minuten)
                if time.time() - download_start_time > 300:
                    raise Exception("Download-Timeout (5 Minuten)")

                # Dateigröße-Check
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
                if total_bytes:
                    size_mb = total_bytes / (1024 * 1024)
                    if size_mb > MAX_FILE_SIZE_MB:
                        raise Exception(f"Datei zu groß ({size_mb:.1f}MB). Maximum: {MAX_FILE_SIZE_MB}MB")
                    downloaded = d.get('downloaded_bytes', 0)
                    if total_bytes > 0:
                        percent = (downloaded / total_bytes) * 100
                        progress_callback(min(int(percent), 99))
                else:
                    # Fallback anhand _percent_str
                    percent_str = d.get('_percent_str')
                    if percent_str:
                        try:
                            percent = float(percent_str.replace('%', '').strip())
                            progress_callback(min(int(percent), 99))
                        except:
                            pass
            elif d.get('status') == 'finished':
                print(f"Download abgeschlossen: {d.get('filename', 'Unknown')}")

        # Ziel: MP3. Quelle: bestaudio (egal, m4a/webm/etc.), danach zu MP3 extrahieren.
        # Formatauswahl: zuerst Audio-only, dann HLS-/HTTPS-, dann generische Fallbacks
        preferred_formats = [
            # 1) Audio-only strikt (keine Video-Spur)
            "bestaudio[vcodec=none][acodec!=none]/bestaudio",
            # 2) M4A/AAC bevorzugt
            "bestaudio[ext=m4a]/bestaudio",
            # 3) WebM/Opus als Fallback
            "bestaudio[ext=webm]/bestaudio",
            # 4) HLS-Audio
            "bestaudio[proto*=m3u8]/bestaudio",
            # 5) Generisch: bestaudio/best
            "bestaudio/best",
            # 6) Letzter Ausweg: best (Video+Audio), danach Audio extrahieren
            "best"
        ]

        last_error = None
        info = None

        for idx, fmt in enumerate(preferred_formats, start=1):
            print(f"Versuche Audio-Format ({idx}/{len(preferred_formats)}): {fmt}")

            ydl_opts = {
                'format': fmt,
                'postprocessors': [
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '0',
                    }
                ],
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'progress_hooks': [progress_hook] if progress_callback else [],
                'quiet': False,
                'no_warnings': False,
                'ignoreerrors': False,
                'extract_flat': False,
                # Keine Formate vorab ausschließen; mehrere Clients erlauben; fehlende_pot tolerieren
                'extractor_args': {
                    'youtube': {
                        # Probiere mehrere Clients; Reihenfolge von stabil zu "breiter"
                        'player_client': ['web', 'web_embedded', 'ios', 'tv', 'web_creator', 'android'],
                        'formats': ['missing_pot'],
                        # Weniger strenge Skips; erlaube dash/hls, da oft nur so Audio verfügbar ist
                        'skip': [],
                        'prefer_free_formats': True
                    }
                },
                # MP3-Ziel: Priorisiere Audio-only, dann Qualität, ohne Protokoll zu erzwingen
                # Vereinfachte Sortierung: priorisiere Audio-only (kein Video), überlasse Rest yt-dlp
                'format_sort': [
                    'hasaud', 'vcodec:None'
                ],
                'socket_timeout': 45,
                'retries': 5,
                'fragment_retries': 5,
                'http_chunk_size': 10485760,
                'no_check_certificate': False,
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print("Teste URL-Verfügbarkeit...")
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        raise Exception("Konnte Video-Info nicht extrahieren")

                    # Dauer-Check (optional, schon an anderer Stelle vorhanden)
                    duration = info.get('duration') or 0
                    if duration and duration > MAX_VIDEO_DURATION:
                        raise Exception("Video zu lang (max. 1 Stunde)")

                    # Versuch 1: Download mit gewähltem Format
                    print(f"Starte Download mit Format: {fmt}")
                    try:
                        ydl.download([url])
                    except yt_dlp.utils.DownloadError as de1:
                        # Wenn Format nicht verfügbar, probiere gezielt generisches Fallback
                        if 'requested format is not available' in str(de1).lower():
                            print("Format nicht verfügbar – erzwungenes Fallback auf bestaudio/best und dann best.")
                            try:
                                ydl.params['format'] = 'bestaudio/best'
                                ydl.download([url])
                            except Exception as de2:
                                print(f"Fallback bestaudio/best scheiterte: {de2}")
                                ydl.params['format'] = 'best'
                                ydl.download([url])
                        else:
                            raise
                    # Erfolgreich heruntergeladen, breche Format-Schleife ab
                    break

            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                print(f"DownloadError bei Format '{fmt}': {msg}")
                last_error = e
                lower = msg.lower()
                # Diagnose-Listing und progressive Fallback-Strategie
                try:
                    print("Debug: Liste verfügbare Formate nach Fehler...")
                    list_available_formats(url)
                except Exception as _e:
                    print(f"Formate-Listing fehlgeschlagen: {_e}")

                # Bei "only images" oder "requested format" sofort nächstes Format versuchen
                if 'only images are available' in lower or 'requested format is not available' in lower:
                    print("Wechsle zum nächsten Fallback-Format...")
                    continue

                # PO-Token/403: Clients werden bereits ohne android priorisiert – nächstes Format versuchen
                if 'po token' in lower or '403' in lower:
                    print("PO-Token/403-Hinweis – nächstes Fallback-Format...")
                    continue

                # Sonstige Fehler: ebenfalls weiter
                print("Nicht-Format-Fehler – nächstes Fallback-Format...")
                continue
            except Exception as e:
                print(f"Unerwarteter Fehler bei Format '{fmt}': {e}")
                last_error = e
                continue
        else:
            # Schleife ohne Break beendet -> kein Erfolg
            if last_error:
                raise last_error
            raise Exception("Audio-Download fehlgeschlagen (keine passenden Formate)")

        # Suche nach erzeugten Dateien (nur MP3 ausgeben)
        print(f"Suche nach Dateien in: {temp_dir}")
        mp3_files = []
        other_audio = []
        for file in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, file)
            print(f"Gefundene Datei: {file} (Größe: {os.path.getsize(file_path)} bytes)")
            if file.endswith('.mp3'):
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if file_size_mb > MAX_FILE_SIZE_MB:
                    os.remove(file_path)
                    raise Exception(f"Datei zu groß ({file_size_mb:.1f}MB)")
                mp3_files.append((file_path, file))
            elif file.endswith(('.m4a', '.webm', '.ogg', '.aac', '.wav', '.mp4', '.m4b')):
                other_audio.append((file_path, file))

        # Falls durch Postprocessor (ausnahmsweise) kein MP3 erzeugt wurde, versuche Konvertierung on-the-fly
        if not mp3_files and other_audio:
            try:
                src_path, src_name = other_audio[0]
                mp3_out = os.path.join(os.path.dirname(src_path), os.path.splitext(src_name)[0] + ".mp3")
                import subprocess
                # Konvertiere sicher zu MP3 (320k CBR) via ffmpeg
                cmd = ["ffmpeg", "-y", "-i", src_path, "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", mp3_out]
                print(f"FFmpeg Fallback-Konvertierung: {' '.join(cmd)}")
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if os.path.exists(mp3_out):
                    mp3_files.append((mp3_out, os.path.basename(mp3_out)))
                    # Quelle ggf. entfernen, um Platz zu sparen
                    try:
                        os.remove(src_path)
                    except:
                        pass
            except Exception as conv_err:
                print(f"FFmpeg Fallback-Konvertierung fehlgeschlagen: {conv_err}")

        if not mp3_files:
            # Zusätzlicher Rettungsanker: Wenn yt-dlp temporäre Fragmente abgelegt hat (z.B. .mp4/.mkv/.ts),
            # versuche die größte Audio-/Container-Datei zu MP3 zu konvertieren.
            try:
                candidates = []
                for file in os.listdir(temp_dir):
                    if file.lower().endswith(('.mp4', '.mkv', '.ts', '.m4a', '.webm', '.ogg', '.aac', '.wav')):
                        path = os.path.join(temp_dir, file)
                        candidates.append((os.path.getsize(path), path, file))
                if candidates:
                    candidates.sort(reverse=True)
                    _, src_path, src_name = candidates[0]
                    mp3_out = os.path.join(os.path.dirname(src_path), os.path.splitext(src_name)[0] + ".mp3")
                    import subprocess
                    cmd = ["ffmpeg", "-y", "-i", src_path, "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", mp3_out]
                    print(f"FFmpeg Rettungs-Konvertierung: {' '.join(cmd)}")
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if os.path.exists(mp3_out):
                        mp3_files.append((mp3_out, os.path.basename(mp3_out)))
                        try:
                            os.remove(src_path)
                        except:
                            pass
            except Exception as e_conv:
                print(f"Rettungs-Konvertierung fehlgeschlagen: {e_conv}")

        if not mp3_files:
            raise Exception("Keine MP3-Datei nach Download gefunden")

        # Erstes MP3 wählen
        mp3_files.sort(key=lambda t: len(t[1]))
        file_path, filename = mp3_files[0]

        title = None
        if info:
            title = info.get('title')
        if not title:
            title = os.path.splitext(filename)[0]

        if progress_callback:
            progress_callback(100)

        print(f"Download erfolgreich: {file_path}")
        return file_path, title

    except Exception as e:
        print(f"Download-Funktion Fehler: {str(e)}")
        return None, str(e)
    finally:
        if st.session_state.active_downloads > 0:
            st.session_state.active_downloads -= 1
        gc.collect()

def extract_mix_playlist_info(url):
    """Extrahiere Videos aus YouTube Mix/Radio Playlists"""
    try:
        print(f"Versuche Mix-Extraktion für: {url}")
        
        # Verschiedene Mix-URL-Formate versuchen
        mix_urls = [
            url,  # Original URL
            url.replace('&start_radio=1', ''),  # Ohne start_radio Parameter
        ]
        
        for attempt, test_url in enumerate(mix_urls):
            print(f"Mix-URL Versuch {attempt + 1}: {test_url}")
            
            try:
                ydl_opts = {
                    'quiet': False,  # Mehr Ausgabe für Debug
                    'no_warnings': False,
                    'extract_flat': True,
                    'socket_timeout': 45,
                    'ignoreerrors': True,
                    'playlistend': MAX_MIX_SIZE,  # Limitiere auf 15 Songs für Mix
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['web', 'android'],
                            'skip': ['dash', 'hls']
                        }
                    }
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                    
                    print(f"Mix-Info extrahiert, Typ: {info.get('_type', 'unknown')}")
                    
                    # Prüfe verschiedene Strukturen
                    if info and info.get('_type') == 'playlist' and 'entries' in info:
                        entries = info['entries']
                        print(f"Mix-Playlist gefunden mit {len(entries)} Einträgen")
                        
                        # Filtere gültige Einträge
                        valid_entries = []
                        for entry in entries:
                            if entry and entry.get('id') and entry.get('title'):
                                valid_entries.append(entry)
                        
                        if valid_entries:
                            return process_mix_entries(info, valid_entries)
                    
                    # Fallback: Versuche als einzelnes Video mit Vorschlägen
                    elif info and info.get('_type') in ['video', 'url_transparent']:
                        print("Mix als Video erkannt, versuche Vorschläge zu extrahieren")
                        return extract_mix_from_video_page(test_url, info)
                    
            except Exception as e:
                print(f"Mix-Extraktion Versuch {attempt + 1} fehlgeschlagen: {str(e)}")
                continue
        
        return None
        
    except Exception as e:
        print(f"Kritischer Fehler bei Mix-Extraktion: {str(e)}")
        return None

def process_mix_entries(info, entries):
    """Verarbeite Mix-Einträge zu unserem Format"""
    try:
        mix_title = info.get('title', 'YouTube Mix')
        mix_uploader = 'YouTube Mix'
        
        # Erkenne Mix-Typ
        if 'Radio' in mix_title:
            mix_type = 'Radio'
        elif 'Mix' in mix_title:
            mix_type = 'Mix'
        else:
            mix_type = 'Automatische Playlist'
        
        videos = []
        for i, entry in enumerate(entries[:MAX_MIX_SIZE]):  # Limitiere auf 15 Songs
            if entry and entry.get('id'):
                # Sichere Dauer-Behandlung
                duration = entry.get('duration', 0)
                if duration is None:
                    duration = 0
                
                video_info = {
                    'id': entry['id'],
                    'title': entry.get('title', f'Song {i+1}'),
                    'duration': duration,
                    'uploader': entry.get('uploader', entry.get('channel', 'Unbekannt')),
                    'url': f"https://www.youtube.com/watch?v={entry['id']}"
                }
                videos.append(video_info)
        
        if videos:
            return {
                'title': f"{mix_type}: {mix_title}",
                'uploader': mix_uploader,
                'video_count': len(videos),
                'videos': videos,
                'is_mix': True,
                'mix_type': mix_type
            }
        
        return None
        
    except Exception as e:
        print(f"Fehler beim Verarbeiten der Mix-Einträge: {str(e)}")
        return None

def extract_mix_from_video_page(url, video_info):
    """Alternative Methode: Extrahiere Mix aus Video-Seite"""
    try:
        print("Versuche Mix-Extraktion über Video-Seite")
        
        # Erstelle Mix-URL basierend auf Video-ID
        video_id = video_info.get('id')
        if not video_id:
            return None
        
        # Verschiedene Mix-URL-Varianten erstellen
        mix_url_variants = [
            f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}",
            f"https://www.youtube.com/playlist?list=RD{video_id}",
            url  # Original URL
        ]
        
        for variant_url in mix_url_variants:
            try:
                print(f"Teste Mix-Variante: {variant_url}")
                
                ydl_opts = {
                    'quiet': True,
                    'extract_flat': True,
                    'playlistend': MAX_MIX_SIZE,
                    'socket_timeout': 30,
                    'ignoreerrors': True,
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['web'],
                        }
                    }
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    mix_info = ydl.extract_info(variant_url, download=False)
                    
                    if mix_info and mix_info.get('_type') == 'playlist' and mix_info.get('entries'):
                        print(f"Mix-Variante erfolgreich: {len(mix_info['entries'])} Einträge")
                        return process_mix_entries(mix_info, mix_info['entries'])
            
            except Exception as e:
                print(f"Mix-Variante fehlgeschlagen: {str(e)}")
                continue
        
        # Fallback: Erstelle minimalen Mix mit dem ursprünglichen Video
        return create_single_video_mix(video_info)
        
    except Exception as e:
        print(f"Fehler bei Mix-Extraktion über Video-Seite: {str(e)}")
        return None

def create_single_video_mix(video_info):
    """Erstelle Mix mit nur einem Video als Fallback"""
    try:
        # Sichere Dauer-Behandlung
        duration = video_info.get('duration', 0)
        if duration is None:
            duration = 0
            
        return {
            'title': f"Mix: {video_info.get('title', 'Unbekannt')}",
            'uploader': 'YouTube Mix (Einzelvideo)',
            'video_count': 1,
            'videos': [{
                'id': video_info.get('id'),
                'title': video_info.get('title', 'Unbekannt'),
                'duration': duration,
                'uploader': video_info.get('uploader', 'Unbekannt'),
                'url': f"https://www.youtube.com/watch?v={video_info.get('id')}"
            }],
            'is_mix': True,
            'mix_type': 'Einzelvideo',
            'note': 'Nur das Startvideo konnte extrahiert werden'
        }
    except:
        return None

def handle_special_youtube_urls(url):
    """Erweiterte Behandlung spezieller YouTube-URL-Typen inklusive Mix"""
    try:
        # Extrahiere Parameter
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        
        playlist_id = query_params.get('list', [None])[0]
        video_id = query_params.get('v', [None])[0]
        
        if playlist_id:
            if playlist_id == 'LL':
                return {
                    'type': 'liked_videos',
                    'message': 'Liked Videos Liste erkannt',
                    'action': 'convert_to_single_video',
                    'video_id': video_id
                }
            
            elif playlist_id.startswith('RD'):
                return {
                    'type': 'radio_mix',
                    'message': 'YouTube Radio/Mix erkannt',
                    'action': 'extract_mix_playlist',  # Neue Aktion für Mix
                    'video_id': video_id,
                    'playlist_id': playlist_id
                }
            
            elif playlist_id.startswith(('UL', 'UU')):
                return {
                    'type': 'uploads_playlist',
                    'message': 'Kanal-Uploads Liste erkannt',
                    'action': 'not_supported',
                    'video_id': video_id
                }
            
            elif playlist_id.startswith('WL'):
                return {
                    'type': 'watch_later',
                    'message': 'Watch Later Liste erkannt',
                    'action': 'not_supported',
                    'video_id': video_id
                }
        
        return None
        
    except Exception as e:
        print(f"Fehler bei spezieller URL-Behandlung: {str(e)}")
        return None

def show_special_url_message(special_info):
    """Erweiterte Nachricht für spezielle URL-Typen inklusive Mix"""
    url_type = special_info['type']
    message = special_info['message']
    action = special_info['action']
    video_id = special_info.get('video_id')
    playlist_id = special_info.get('playlist_id')
    
    if url_type == 'liked_videos':
        st.info("📋 **Liked Videos Liste erkannt**")
        st.warning("**Liked Videos Listen sind privat und können nicht als Playlist heruntergeladen werden.**")
        
        if video_id:
            st.success("💡 **Automatische Lösung:** Das einzelne Video aus der URL wird stattdessen heruntergeladen.")
            return f"https://www.youtube.com/watch?v={video_id}"
        else:
            st.error("❌ Keine Video-ID in der URL gefunden.")
            st.info("**Alternativen:**")
            st.write("• Öffnen Sie ein Video aus Ihrer Liked Videos Liste")
            st.write("• Kopieren Sie die URL des einzelnen Videos")
            st.write("• Erstellen Sie eine öffentliche Playlist mit Ihren Lieblingssongs")
    
    elif url_type == 'radio_mix':
        st.info("📻 **YouTube Radio/Mix erkannt**")
        st.success("**🎵 Mix-Playlist wird extrahiert!**")
        st.info("**Das System versucht, bis zu 15 Songs aus dem Mix zu laden:**")
        st.write("• Startet mit dem aktuellen Song")
        st.write("• Lädt die nächsten vorgeschlagenen Songs")
        st.write("• Mix-Inhalte können variieren")
        st.write("• Automatisch generierte Zusammenstellung")
        
        # Rückgabe der Original-URL für Mix-Verarbeitung
        return 'EXTRACT_MIX'
    
    elif url_type in ['uploads_playlist', 'watch_later']:
        type_names = {
            'uploads_playlist': 'Kanal-Uploads',
            'watch_later': 'Watch Later'
        }
        
        st.info(f"📂 **{type_names[url_type]} Liste erkannt**")
        st.warning(f"**{type_names[url_type]} Listen können nicht als Playlist heruntergeladen werden.**")
        
        if video_id:
            st.success("💡 **Automatische Lösung:** Das einzelne Video aus der URL wird stattdessen heruntergeladen.")
            return f"https://www.youtube.com/watch?v={video_id}"
        else:
            st.info("**Alternativen:**")
            st.write("• Laden Sie einzelne Videos separat herunter")
            st.write("• Erstellen Sie eine eigene öffentliche Playlist")
            st.write("• Suchen Sie nach kurierten Playlists zu diesem Thema")
    
    return None

def clean_youtube_url(url):
    """Verbesserte URL-Bereinigung für alle YouTube-URL-Typen"""
    if not url:
        return url
    
    try:
        url = url.strip()
        
        # Sicherheitscheck: Nur YouTube URLs erlauben
        if not any(domain in url.lower() for domain in ['youtube.com', 'youtu.be']):
            return None
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        parsed = urlparse(url)
        
        if 'youtu.be' in parsed.netloc:
            video_id = parsed.path.lstrip('/')
            if len(video_id) == 11:
                return f"https://www.youtube.com/watch?v={video_id}"
        
        elif 'youtube.com' in parsed.netloc:
            query_params = parse_qs(parsed.query)
            
            # Spezielle Behandlung für verschiedene URL-Typen
            if 'list' in query_params:
                playlist_id = query_params['list'][0]
                
                # Prüfe auf spezielle Playlist-Typen
                if playlist_id in ['LL']:  # Liked Videos
                    # Behandle als Einzelvideo, da LL-Playlists privat sind
                    if 'v' in query_params:
                        video_id = query_params['v'][0]
                        if len(video_id) == 11:
                            return f"https://www.youtube.com/watch?v={video_id}"
                    return None  # LL ohne Video-ID nicht unterstützt
                
                elif playlist_id.startswith('RD'):  # Radio/Mix Playlists
                    # Behalte Original-URL für Mix-Extraktion
                    return url
                
                else:
                    # Normale Playlist
                    if 'v' in query_params:
                        video_id = query_params['v'][0]
                        return f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
                    else:
                        return f"https://www.youtube.com/playlist?list={playlist_id}"
            
            elif 'v' in query_params:
                # Einzelvideo-URL
                video_id = query_params['v'][0]
                if len(video_id) == 11:
                    return f"https://www.youtube.com/watch?v={video_id}"
        
        return url
        
    except Exception as e:
        print(f"URL-Bereinigungsfehler: {str(e)}")
        return None

def is_playlist_url(url):
    """Erweiterte Playlist-Erkennung inklusive Mix"""
    if not url:
        return False
    
    url_lower = url.lower()
    
    # Playlist-Indikatoren finden
    playlist_indicators = [
        'list=',
        'playlist?',
        '/playlist',
        '&list=',
        '?list='
    ]
    
    has_playlist = any(indicator in url_lower for indicator in playlist_indicators)
    
    if not has_playlist:
        return False
    
    # Extrahiere Playlist-ID für weitere Prüfung
    playlist_id_match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
    if playlist_id_match:
        playlist_id = playlist_id_match.group(1)
        
        # Spezielle Behandlung für Mix-Playlists - diese werden als Playlist behandelt
        if playlist_id.startswith('RD'):
            return True  # Mix-Playlists werden jetzt unterstützt
        
        # Schließe andere spezielle Playlist-Typen aus
        if playlist_id in ['LL'] or playlist_id.startswith(('UL', 'UU', 'WL')):
            return False
    
    return True

def extract_playlist_info(url):
    """Extrahiere Playlist-Informationen mit erweiterter Fehlerbehandlung"""
    try:
        # Playlist-ID extrahieren
        playlist_id_match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
        if not playlist_id_match:
            print("Keine Playlist-ID in URL gefunden")
            return {'error': 'no_playlist_id', 'message': 'Keine gültige Playlist-ID gefunden'}
        
        playlist_id = playlist_id_match.group(1)
        print(f"Extrahierte Playlist-ID: {playlist_id}")
        
        # Spezielle Behandlung für Mix-Playlists
        if playlist_id.startswith('RD'):
            print("Mix-Playlist erkannt, verwende spezielle Extraktion")
            return extract_mix_playlist_info(url)
        
        # Erweiterte Prüfung auf andere problematische Playlist-Typen
        if playlist_id == 'LL':
            return {
                'error': 'liked_videos_playlist',
                'message': 'Liked Videos Listen sind privat',
                'playlist_id': playlist_id
            }
        elif playlist_id.startswith(('UL', 'UU')):
            return {
                'error': 'uploads_playlist',
                'message': 'Upload-Playlists werden nicht unterstützt',
                'playlist_id': playlist_id
            }
        elif playlist_id.startswith('WL'):
            return {
                'error': 'watch_later_playlist',
                'message': 'Watch Later Listen sind privat',
                'playlist_id': playlist_id
            }
        
        # Prüfe auf alte/ungültige Playlist-IDs (sehr kurz oder ungewöhnliche Zeichen)
        if len(playlist_id) < 16 or not re.match(r'^[a-zA-Z0-9_-]+$', playlist_id):
            return {
                'error': 'invalid_playlist_id',
                'message': 'Ungültige Playlist-ID Format',
                'playlist_id': playlist_id
            }
        
        # Versuche verschiedene Playlist-URLs
        playlist_urls = [
            f"https://www.youtube.com/playlist?list={playlist_id}",
            f"https://youtube.com/playlist?list={playlist_id}",
        ]
        
        for attempt, test_url in enumerate(playlist_urls):
            print(f"Versuche URL {attempt + 1}: {test_url}")
            
            try:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'socket_timeout': 30,
                    'ignoreerrors': False,
                    'playlistend': MAX_PLAYLIST_SIZE,
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['web', 'android'],
                            'skip': ['dash', 'hls']
                        }
                    }
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                    
                    if info and info.get('_type') == 'playlist' and 'entries' in info:
                        print(f"✅ Playlist gefunden mit {len(info['entries'])} Einträgen")
                        return process_playlist_entries(info)
                    
            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e).lower()
                print(f"yt-dlp Fehler: {error_msg}")
                
                if 'does not exist' in error_msg or 'not found' in error_msg:
                    return {
                        'error': 'playlist_not_found',
                        'message': 'Playlist existiert nicht oder ist nicht öffentlich zugänglich',
                        'playlist_id': playlist_id,
                        'detailed_error': str(e)
                    }
                elif 'private' in error_msg or 'unavailable' in error_msg:
                    return {
                        'error': 'playlist_private',
                        'message': 'Playlist ist privat oder nicht verfügbar',
                        'playlist_id': playlist_id,
                        'detailed_error': str(e)
                    }
                else:
                    # Unbekannter Fehler - versuche nächste URL
                    continue
                    
            except Exception as e:
                print(f"Unerwarteter Fehler: {str(e)}")
                continue
        
        # Wenn alle Versuche fehlschlagen
        return {
            'error': 'extraction_failed',
            'message': 'Playlist konnte nicht geladen werden',
            'playlist_id': playlist_id
        }
        
    except Exception as e:
        print(f"Kritischer Fehler bei Playlist-Extraktion: {str(e)}")
        return {
            'error': 'critical_error',
            'message': f'Kritischer Fehler: {str(e)}'
        }

def process_playlist_entries(info):
    """Verarbeite Playlist-Einträge zu unserem Format"""
    try:
        entries = info.get('entries', [])
        playlist_title = info.get('title', info.get('playlist_title', 'Unbekannte Playlist'))
        playlist_uploader = info.get('uploader', info.get('channel', info.get('uploader_id', 'Unbekannt')))
        
        videos = []
        for i, entry in enumerate(entries):
            if i >= MAX_PLAYLIST_SIZE:
                break
            
            if entry and entry.get('id'):
                # Sichere Dauer-Behandlung
                duration = entry.get('duration', 0)
                if duration is None:
                    duration = 0
                    
                video_info = {
                    'id': entry['id'],
                    'title': entry.get('title', f'Video {i+1}'),
                    'duration': duration,
                    'uploader': entry.get('uploader', entry.get('channel', 'Unbekannt')),
                    'url': f"https://www.youtube.com/watch?v={entry['id']}"
                }
                videos.append(video_info)
        
        if videos:
            return {
                'title': playlist_title,
                'uploader': playlist_uploader,
                'video_count': len(videos),
                'videos': videos
            }
        
        return None
        
    except Exception as e:
        print(f"Fehler beim Verarbeiten der Playlist-Einträge: {str(e)}")
        return None

def handle_playlist_url(cleaned_url):
    """Erweiterte Playlist-Behandlung inklusive Mix-Support"""
    
    # Playlist-Info laden
    if not st.session_state.playlist_videos or st.session_state.get('last_playlist_url') != cleaned_url:
        
        # Debug-Informationen
        playlist_id_match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', cleaned_url)
        playlist_id = playlist_id_match.group(1) if playlist_id_match else "Unbekannt"
        
        # Erkenne Playlist-Typ
        is_mix = playlist_id.startswith('RD')
        playlist_type = "Mix/Radio" if is_mix else "Normal"
        
        with st.expander("🔧 Debug-Informationen", expanded=False):
            st.code(f"URL: {cleaned_url}")
            st.code(f"Playlist-ID: {playlist_id}")
            st.code(f"ID-Länge: {len(playlist_id)}")
            st.code(f"Playlist-Typ: {playlist_type}")
            if is_mix:
                st.code(f"Mix-Limit: {MAX_MIX_SIZE} Songs")
        
        # Fortschrittsanzeige
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            if is_mix:
                status_text.text("🎵 Analysiere Mix-Playlist...")
                progress_bar.progress(20)
                
                status_text.text("📡 Extrahiere Mix-Songs...")
                progress_bar.progress(50)
                
                # Versuche Mix-Extraktion
                playlist_info = extract_mix_playlist_info(cleaned_url)
                progress_bar.progress(90)
                
                if playlist_info and playlist_info.get('videos'):
                    st.session_state.playlist_videos = playlist_info['videos']
                    st.session_state.last_playlist_url = cleaned_url
                    st.session_state.selected_videos = []
                    
                    progress_bar.progress(100)
                    status_text.text("✅ Mix erfolgreich geladen!")
                    
                    # Spezielle Mix-Nachricht
                    st.success(f"🎵 **Mix geladen:** {playlist_info['title']}")
                    
                    mix_type = playlist_info.get('mix_type', 'Mix')
                    video_count = playlist_info['video_count']
                    
                    if playlist_info.get('note'):
                        st.warning(f"ℹ️ {playlist_info['note']}")
                    
                    st.info(f"📻 Typ: {mix_type} | 🎵 Songs: {video_count}")
                    
                    if video_count > 1:
                        st.info("💡 **Mix-Hinweise:**")
                        st.write("• Mix-Inhalte werden dynamisch generiert")
                        st.write("• Die Reihenfolge kann variieren")
                        st.write(f"• Bis zu {video_count} Songs wurden erfasst")
                        st.write("• Basiert auf YouTube-Algorithmus")
                    else:
                        st.warning("⚠️ Nur das Startvideo konnte extrahiert werden")
                        st.info("**Mögliche Gründe:**")
                        st.write("• Mix ist noch nicht vollständig generiert")
                        st.write("• Temporäre YouTube-Einschränkungen")
                        st.write("• Mix-Algorithmus hat nicht genug ähnliche Songs gefunden")
                    
                    # Aufräumen
                    time.sleep(1)
                    progress_bar.empty()
                    status_text.empty()
                    
                    return True
                
                else:
                    progress_bar.empty()
                    status_text.empty()
                    
                    st.error("❌ Mix-Extraktion fehlgeschlagen")
                    st.warning("**Mögliche Probleme:**")
                    st.write("• Mix ist nicht öffentlich verfügbar")
                    st.write("• Temporäre YouTube-Einschränkungen")
                    st.write("• Mix wurde dynamisch verändert")
                    st.write("• Nicht genug ähnliche Songs für Mix verfügbar")
                    
                    st.info("**Alternativen:**")
                    st.write("• Versuchen Sie es in ein paar Minuten erneut")
                    st.write("• Nutzen Sie das einzelne Video aus der URL")
                    st.write("• Erstellen Sie eine manuelle Playlist")
                    st.write("• Suchen Sie nach ähnlichen öffentlichen Playlists")
                    
                    return False
            
            else:
                # Normale Playlist-Behandlung
                status_text.text("🔍 Prüfe Playlist-Verfügbarkeit...")
                progress_bar.progress(25)
                
                status_text.text("📡 Lade Playlist-Daten...")
                progress_bar.progress(50)
                
                playlist_info = extract_playlist_info(cleaned_url)
                progress_bar.progress(100)
                
                # Erfolgreiche Extraktion
                if playlist_info and playlist_info.get('videos') and not playlist_info.get('error'):
                    st.session_state.playlist_videos = playlist_info['videos']
                    st.session_state.last_playlist_url = cleaned_url
                    st.session_state.selected_videos = []
                    
                    status_text.text("✅ Playlist erfolgreich geladen!")
                    
                    st.success(f"✅ Playlist geladen: **{playlist_info['title']}**")
                    st.info(f"📺 Kanal: {playlist_info['uploader']} | 🎵 Videos: {playlist_info['video_count']}")
                    
                    # Aufräumen
                    time.sleep(1)
                    progress_bar.empty()
                    status_text.empty()
                    
                    return True
                
                # Spezifische Fehlerbehandlung (bestehender Code)
                elif playlist_info and playlist_info.get('error'):
                    progress_bar.empty()
                    status_text.empty()
                    
                    error_type = playlist_info['error']
                    error_message = playlist_info.get('message', 'Unbekannter Fehler')
                    
                    if error_type == 'playlist_not_found':
                        st.error("❌ Playlist nicht gefunden")
                        st.warning("**Diese Playlist mit der ID `{0}` existiert nicht oder ist nicht öffentlich zugänglich.**".format(playlist_info.get('playlist_id', 'Unbekannt')))
                        
                        st.info("**Mögliche Ursachen:**")
                        st.write("• Playlist wurde vom Ersteller gelöscht")
                        st.write("• Playlist ist auf 'Privat' oder 'Nicht gelistet' gesetzt")
                        st.write("• Playlist-URL enthält einen Tippfehler")
                        st.write("• Die Playlist-ID ist veraltet oder ungültig")
                        st.write("• Geografische Einschränkungen")
                        
                        st.info("**Was Sie tun können:**")
                        st.write("1. Überprüfen Sie die URL auf Tippfehler")
                        st.write("2. Versuchen Sie, die Playlist im Browser zu öffnen")
                        st.write("3. Fragen Sie den Ersteller nach dem aktuellen Link")
                        st.write("4. Nutzen Sie eine andere öffentliche Playlist")
                        
                    elif error_type in ['liked_videos_playlist', 'uploads_playlist', 'watch_later_playlist']:
                        error_types = {
                            'liked_videos_playlist': 'Liked Videos',
                            'uploads_playlist': 'Upload-Playlist',
                            'watch_later_playlist': 'Watch Later'
                        }
                        st.error(f"🚫 {error_types[error_type]} Playlist")
                        st.warning(f"**{error_message}**")
                        st.info("Diese Playlist-Typen können nicht heruntergeladen werden.")
                        
                    elif error_type == 'playlist_private':
                        st.error("🔒 Private Playlist")
                        st.warning("Diese Playlist ist privat und kann nicht heruntergeladen werden.")
                        st.info("**Lösung:** Bitten Sie den Ersteller, die Playlist auf 'Öffentlich' zu setzen.")
                        
                    elif error_type == 'playlist_unavailable':
                        st.error("⏰ Playlist temporär nicht verfügbar")
                        st.warning("Die Playlist ist momentan nicht erreichbar.")
                        st.info("**Lösung:** Versuchen Sie es in ein paar Minuten erneut.")
                        
                    elif error_type == 'invalid_playlist_id':
                        st.error("🚫 Ungültiges Playlist-ID Format")
                        st.warning(f"Die Playlist-ID `{playlist_info.get('playlist_id', 'Unbekannt')}` hat ein ungültiges Format.")
                        st.info("**Korrekte Playlist-IDs:**")
                        st.write("• Sind normalerweise 26-34 Zeichen lang")
                        st.write("• Enthalten nur Buchstaben, Zahlen, Bindestriche und Unterstriche")
                        st.write("• Beginnen oft mit 'PL', 'UU', 'LL', 'RD' oder ähnlichen Präfixen")
                        
                    else:
                        st.error(f"❌ {error_message}")
                    
                    # Erweiterte Debug-Informationen bei Fehlern
                    if playlist_info.get('detailed_error'):
                        with st.expander("🔍 Technische Fehlerdetails"):
                            st.code(f"Playlist-ID: {playlist_info.get('playlist_id', 'Unbekannt')}")
                            st.code(f"Fehlertyp: {error_type}")
                            st.code(f"Detaillierter Fehler: {playlist_info['detailed_error']}")
                    
                    return False
                
                else:
                    progress_bar.empty()
                    status_text.empty()
                    st.error("❌ Unbekannter Fehler beim Laden der Playlist")
                    return False
                
        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            
            st.error("❌ Kritischer Fehler beim Laden der Playlist")
            
            with st.expander("🐛 Technische Details"):
                st.code(f"Fehler: {str(e)}")
                st.code(f"URL: {cleaned_url}")
                st.code(f"Playlist-ID: {playlist_id}")
                st.code(f"Typ: {playlist_type}")
            
            return False
    
    return True

def suggest_alternative_playlists():
    """Erweiterte Vorschläge für funktionierende Playlists"""
    st.info("**Testen Sie mit diesen funktionierenden Beispiel-Playlists:**")
    
    example_playlists = [
        {
            'title': 'Lofi Hip Hop Music',
            'url': 'https://www.youtube.com/playlist?list=PLOHoVaTp8R7eZNSOxP6rPpfuAk5_5MaZh',
            'description': 'Entspannungsmusik zum Arbeiten und Lernen'
        },
        {
            'title': 'Top 50 Global',
            'url': 'https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI',
            'description': 'Aktuelle Chart-Hits weltweit'
        },
        {
            'title': 'Classical Music Collection',
            'url': 'https://www.youtube.com/playlist?list=PLq3yPcz7gPq3BrZe-jBhZP2VVB4YQC2F3',
            'description': 'Klassische Meisterwerke'
        },
        {
            'title': 'Meditation & Sleep Music',
            'url': 'https://www.youtube.com/playlist?list=PLQ1YHV-gKc3vO9E_uS5Z1_X5qE8-rYwpJ',
            'description': 'Musik für Meditation und besseren Schlaf'
        }
    ]
    
    for i, playlist in enumerate(example_playlists):
        with st.container():
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"**{playlist['title']}**")
                st.caption(playlist['description'])
                st.code(playlist['url'])
            with col2:
                if st.button("Testen", key=f"example_{i}"):
                    st.session_state.last_url = playlist['url']
                    st.session_state.clear_input = True
                    st.session_state.input_key += 1
                    st.rerun()
    
    st.markdown("---")
    st.info("**Tipps für eigene Playlists:**")
    st.write("• Stellen Sie sicher, dass die Playlist auf 'Öffentlich' gesetzt ist")
    st.write("• Verwenden Sie die vollständige Playlist-URL")
    st.write("• Testen Sie zunächst einzelne Videos aus der Playlist")
    st.write("• Mix-Playlists funktionieren jetzt auch (bis zu 15 Songs)")
    st.write("• Manche sehr große Playlists (>1000 Videos) werden möglicherweise nicht vollständig geladen")

def download_multiple_videos(video_urls, progress_callback=None, status_callback=None):
    """Download mehrere Videos mit verbessertem Status-Feedback"""
    downloaded_files = []
    failed_downloads = []
    
    def download_single_video(video_data):
        url, title = video_data
        try:
            if status_callback:
                status_callback(f"Lade: {title[:50]}...")
            
            file_path, actual_title = download_audio_with_progress(url)
            if file_path:
                return file_path, actual_title or title
            else:
                return None, f"Download fehlgeschlagen: {title}"
        except Exception as e:
            return None, f"Fehler bei {title}: {str(e)}"
    
    total_videos = len(video_urls)
    completed = 0
    
    # Sequenzieller Download um Server nicht zu überlasten
    for i, video_data in enumerate(video_urls):
        try:
            if status_callback:
                status_callback(f"Video {i+1}/{total_videos}: {video_data[1][:40]}...")
            
            result = download_single_video(video_data)
            if result[0]:  # Erfolgreicher Download
                downloaded_files.append(result)
                if status_callback:
                    status_callback(f"✅ Erfolgreich: {result[1][:40]}...")
            else:  # Fehlgeschlagener Download
                failed_downloads.append(result[1])
                if status_callback:
                    status_callback(f"❌ Fehlgeschlagen: {video_data[1][:40]}...")
            
            completed += 1
            if progress_callback:
                progress_callback(int((completed / total_videos) * 100))
                
        except Exception as e:
            failed_downloads.append(f"Fehler: {str(e)}")
            completed += 1
            if progress_callback:
                progress_callback(int((completed / total_videos) * 100))
    
    return downloaded_files, failed_downloads

def create_zip_file(file_paths_and_titles, zip_name="playlist_download.zip"):
    """Erstelle ZIP-Datei mit verbesserter Fehlerbehandlung"""
    zip_buffer = io.BytesIO()
    
    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
            for i, (file_path, title) in enumerate(file_paths_and_titles):
                if os.path.exists(file_path):
                    # Bereinige Dateinamen und verhindere Duplikate
                    safe_filename = clean_filename(f"{i+1:02d}_{title}.mp3")
                    
                    # Prüfe ob Datei zu groß ist
                    file_size = os.path.getsize(file_path)
                    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                        print(f"Datei zu groß, überspringe: {safe_filename}")
                        continue
                    
                    zip_file.write(file_path, safe_filename)
                    print(f"Zu ZIP hinzugefügt: {safe_filename}")
                    
                    # Lösche temporäre Datei
                    try:
                        os.remove(file_path)
                    except:
                        pass
                else:
                    print(f"Datei nicht gefunden: {file_path}")
        
        zip_buffer.seek(0)
        zip_data = zip_buffer.getvalue()
        
        # Prüfe ZIP-Größe
        zip_size_mb = len(zip_data) / (1024 * 1024)
        print(f"ZIP erstellt: {zip_size_mb:.2f} MB")
        
        return zip_data
        
    except Exception as e:
        print(f"Fehler beim Erstellen der ZIP-Datei: {str(e)}")
        return None
    finally:
        zip_buffer.close()

def create_zip_download_link(zip_data, filename="playlist_download.zip"):
    """Erstelle Download-Link für ZIP-Datei mit verbessertem Handling"""
    try:
        # Prüfe ZIP-Größe
        zip_size_mb = len(zip_data) / (1024 * 1024)
        print(f"ZIP-Größe: {zip_size_mb:.2f} MB")
        
        if zip_size_mb > MAX_ZIP_SIZE_MB:  # Limit für Browser-Download
            return None, f"ZIP-Datei zu groß ({zip_size_mb:.1f}MB). Maximum für automatischen Download: {MAX_ZIP_SIZE_MB}MB"
        
        b64_data = base64.b64encode(zip_data).decode()
        
        download_script = f"""
        <script>
        function autoDownloadZip() {{
            try {{
                const link = document.createElement('a');
                link.href = 'data:application/zip;base64,{b64_data}';
                link.download = '{filename}';
                link.style.display = 'none';
                document.body.appendChild(link);
                
                // Trigger download
                link.click();
                
                // Clean up
                setTimeout(function() {{
                    document.body.removeChild(link);
                }}, 1000);
                
                console.log('ZIP download triggered successfully');
            }} catch (error) {{
                console.error('ZIP download failed:', error);
                alert('Download fehlgeschlagen. Bitte verwenden Sie den Download-Button.');
            }}
        }}
        
        // Multiple attempts to ensure download
        setTimeout(autoDownloadZip, 500);
        setTimeout(autoDownloadZip, 2000);
        </script>
        """
        
        return download_script, None
        
    except Exception as e:
        return None, f"Fehler beim Erstellen des Downloads: {str(e)}"

def create_streamlit_download_button(zip_data, filename="playlist_download.zip"):
    """Erstelle Streamlit Download-Button als Fallback"""
    try:
        return st.download_button(
            label="📥 ZIP-Datei herunterladen",
            data=zip_data,
            file_name=filename,
            mime="application/zip",
            use_container_width=True,
            type="primary"
        )
    except Exception as e:
        st.error(f"Download-Button Fehler: {str(e)}")
        return False

def get_video_info(url):
    """Hole Video-Informationen ohne Download mit Sicherheitschecks"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 30,
            'extractor_args': {
                'youtube': {
                    # Gleiche Client-Reihenfolge wie im Downloader für Konsistenz
                    'player_client': ['web', 'web_embedded', 'ios', 'tv', 'web_creator', 'android'],
                }
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Dauer-Check
            duration = info.get('duration', 0)
            if duration and duration > MAX_VIDEO_DURATION:
                return None
            
            # Sichere Dauer-Behandlung
            if duration is None:
                duration = 0
            
            return {
                'title': info.get('title', 'Unbekannt'),
                'duration': duration,
                'uploader': info.get('uploader', 'Unbekannt'),
                'view_count': info.get('view_count', 0),
                'thumbnail': info.get('thumbnail', '')
            }
    except Exception as e:
        return None

def format_duration(seconds):
    """Formatiere Dauer in MM:SS Format - korrigierte Version"""
    if seconds:
        try:
            # Sicherstellen, dass seconds ein Integer ist
            seconds = int(float(seconds)) if seconds else 0
            minutes = seconds // 60
            seconds = seconds % 60
            return f"{minutes:02d}:{seconds:02d}"
        except (ValueError, TypeError):
            return "00:00"
    return "00:00"

def is_valid_youtube_url(url):
    """YouTube URL Validierung - unterstützt Video+Playlist Kombinationen"""
    if not url or len(url) < 10:
        return False
    
    url = url.strip()
    
    # Sicherheitscheck: Nur YouTube URLs
    youtube_domains = [
        'youtube.com',
        'www.youtube.com', 
        'm.youtube.com',
        'youtu.be'
    ]
    
    has_youtube_domain = any(domain in url.lower() for domain in youtube_domains)
    if not has_youtube_domain:
        return False
    
    # Zusätzliche Sicherheitschecks
    if any(char in url for char in ['<', '>', '"', "'"]):
        return False
    
    if 'youtu.be/' in url.lower():
        return bool(re.search(r'youtu\.be/[\w-]{11}', url, re.IGNORECASE))
    
    if 'youtube.com' in url.lower():
        # Video+Playlist Kombination
        if 'watch' in url.lower() and ('v=' in url or 'list=' in url):
            return True
        # Nur Playlist
        elif 'playlist' in url.lower() and 'list=' in url:
            return True
        # Nur Video
        elif 'watch' in url.lower() and 'v=' in url:
            video_id_match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', url)
            return bool(video_id_match)
    
    return False

def extract_video_id(url):
    """Extrahiere Video-ID aus YouTube URL"""
    if not url:
        return None
    
    youtu_be_match = re.search(r'youtu\.be/([a-zA-Z0-9_-]{11})', url)
    if youtu_be_match:
        return youtu_be_match.group(1)
    
    youtube_match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', url)
    if youtube_match:
        return youtube_match.group(1)
    
    return url

def clean_filename(filename):
    """Bereinige Dateinamen von ungültigen Zeichen"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    filename = re.sub(r'_+', '_', filename)
    filename = filename.strip('_')
    
    if len(filename) > 200:
        filename = filename[:200]
    
    return filename

def create_download_link_and_clear_input(file_path, filename):
    """Erstelle automatischen Download-Link und leere Input nach Speicherung"""
    with open(file_path, 'rb') as file:
        file_data = file.read()
    
    b64_data = base64.b64encode(file_data).decode()
    
    download_script = f"""
    <script>
    function autoDownload() {{
        const link = document.createElement('a');
        link.href = 'data:audio/mpeg;base64,{b64_data}';
        link.download = '{filename}';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        
        setTimeout(function() {{
            const inputs = document.querySelectorAll('input[type="text"]');
            inputs.forEach(function(input) {{
                if (input.placeholder && input.placeholder.includes('youtube')) {{
                    input.value = '';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }});
        }}, 2000);
    }}
    setTimeout(autoDownload, 1000);
    </script>
    """
    
    return download_script

def inject_hidden_ad_slots():
    """Versteckte Werbeplatzhalter einfügen"""
    return f"""
    <div style="display: none;">
        {AD_SLOT_HEADER}
        {AD_SLOT_SIDEBAR}
        {AD_SLOT_FOOTER}
    </div>
    """

def main():
    st.set_page_config(
        page_title="YouTube Audio Converter",
        page_icon="🎵",
        layout="wide",
        initial_sidebar_state="collapsed"
    )
    
    # CSS zum Verstecken des Deploy Buttons, Menüs und Streamlit Branding
    hide_streamlit_style = """
    <style>
    .stDeployButton {
        display: none !important;
    }
    
    header[data-testid="stHeader"] {
        display: none !important;
    }
    
    [data-testid="stToolbar"] {
        display: none !important;
    }
    
    .stAppHeader {
        display: none !important;
    }
    
    footer {
        visibility: hidden !important;
        height: 0% !important;
    }
    
    #MainMenu {
        visibility: hidden !important;
    }
    
    .stAppFooter {
        display: none !important;
    }
    
    [data-testid="stDecoration"] {
        display: none !important;
    }
    
    [data-testid="stStatusWidget"] {
        display: none !important;
    }
    
    .viewerBadge_container__1QSob {
        display: none !important;
    }
    
    .stAppViewContainer > .main .block-container {
        padding-top: 1rem !important;
    }
    
    .stActionButton {
        display: none !important;
    }
    
    .video-item {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 10px;
        margin: 5px 0;
        background-color: #f9f9f9;
    }
    
    .selected-video {
        background-color: #e3f2fd !important;
        border-color: #2196f3 !important;
    }
    
    .download-section {
        background-color: #f0f8ff;
        padding: 20px;
        border-radius: 10px;
        border: 2px solid #4CAF50;
        margin: 20px 0;
    }
    
    .special-url-info {
        background-color: #fff3cd;
        padding: 15px;
        border-radius: 8px;
        border-left: 4px solid #ffc107;
        margin: 15px 0;
    }
    
    .mix-info {
        background-color: #e8f5e8;
        padding: 15px;
        border-radius: 8px;
        border-left: 4px solid #28a745;
        margin: 15px 0;
    }
    </style>
    """
    
    st.markdown(hide_streamlit_style, unsafe_allow_html=True)
    
    # Periodische Bereinigung alter Daten
    cleanup_old_tracking_data()
    
    # Session ID generieren
    if 'session_id' not in st.session_state:
        st.session_state.session_id = hashlib.md5(str(time.time()).encode()).hexdigest()
    
    # Versteckte Werbeplatzhalter einfügen
    st.components.v1.html(inject_hidden_ad_slots(), height=0)
    
    # Header
    st.title("🎵 YouTube Audio Converter")
    st.markdown("**Schneller MP3 Download von YouTube Videos, Playlists und Mixes**")
    
    # Versteckter Header-Werbeplatz
    st.components.v1.html("<!-- AD_HEADER_PLACEHOLDER -->", height=0)
    
    st.markdown("---")
    
    # Session State initialisieren
    if 'last_video_id' not in st.session_state:
        st.session_state.last_video_id = ""
    if 'download_completed' not in st.session_state:
        st.session_state.download_completed = False
    if 'current_download' not in st.session_state:
        st.session_state.current_download = False
    if 'cleaned_url' not in st.session_state:
        st.session_state.cleaned_url = ""
    if 'auto_download_triggered' not in st.session_state:
        st.session_state.auto_download_triggered = False
    if 'download_finished' not in st.session_state:
        st.session_state.download_finished = False
    if 'file_saved' not in st.session_state:
        st.session_state.file_saved = False
    if 'clear_input' not in st.session_state:
        st.session_state.clear_input = False
    if 'input_key' not in st.session_state:
        st.session_state.input_key = 0
    if 'download_count' not in st.session_state:
        st.session_state.download_count = 0
    if 'is_playlist_mode' not in st.session_state:
        st.session_state.is_playlist_mode = False
    
    # Sicherheitschecks
    client_ip = get_client_ip()
    session_id = get_session_id()
    
    # Systemressourcen prüfen
    resources_ok, resource_msg = check_system_resources()
    if not resources_ok:
        st.error(f"🚫 {resource_msg}")
        st.info("Bitte versuchen Sie es später erneut.")
        return
    
    # Layout
    col1, col2 = st.columns([3, 1])
    
    with col1:
        # URL Input
        url = st.text_input(
            "YouTube URL eingeben (Video, Playlist oder Mix):",
            placeholder="https://www.youtube.com/watch?v=... oder https://www.youtube.com/playlist?list=...",
            key=f"url_input_{st.session_state.input_key}",
            value="" if st.session_state.clear_input else st.session_state.get("last_url", "")
        )
        
        # Reset clear_input flag
        if st.session_state.clear_input:
            st.session_state.clear_input = False
            st.session_state.last_url = ""
        else:
            st.session_state.last_url = url
        
        # URL-Bereinigung und Sicherheitschecks
        if url:
            # Prüfe zuerst auf spezielle URL-Typen
            special_info = handle_special_youtube_urls(url)
            
            if special_info:
                # Zeige spezielle Nachricht und behandle URL
                if special_info['type'] == 'radio_mix':
                    st.markdown('<div class="mix-info">', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="special-url-info">', unsafe_allow_html=True)
                
                converted_url = show_special_url_message(special_info)
                st.markdown('</div>', unsafe_allow_html=True)
                
                if converted_url == 'EXTRACT_MIX':
                    # Mix-Modus - verwende Original-URL
                    cleaned_url = clean_youtube_url(url)
                    is_valid = True
                    is_playlist = True  # Mix wird als Playlist behandelt
                    
                    # Debug-Info für Mix
                    with st.expander("🔧 Mix-URL-Behandlung", expanded=False):
                        st.code(f"Original: {url}")
                        st.code(f"Bereinigt: {cleaned_url}")
                        st.code(f"Typ: {special_info['type']}")
                        st.code(f"Aktion: Mix-Extraktion")
                        st.code(f"Max Songs: {MAX_MIX_SIZE}")
                        
                elif converted_url:
                    # Andere spezielle URLs - verwende konvertierte URL für Einzelvideo-Download
                    cleaned_url = converted_url
                    is_valid = True
                    is_playlist = False
                    
                    # Debug-Info für Konvertierung
                    with st.expander("🔧 URL-Konvertierung", expanded=False):
                        st.code(f"Original: {url}")
                        st.code(f"Konvertiert zu: {cleaned_url}")
                        st.code(f"Typ: {special_info['type']}")
                        st.code(f"Aktion: {special_info['action']}")
                else:
                    # Keine gültige Konvertierung möglich
                    return
            else:
                # Normale URL-Verarbeitung
                cleaned_url = clean_youtube_url(url)
                if not cleaned_url:
                    st.error("🚫 Ungültige URL. Nur YouTube URLs sind erlaubt.")
                    return
                
                is_valid = is_valid_youtube_url(cleaned_url)
                is_playlist = is_playlist_url(cleaned_url)
            
            st.session_state.cleaned_url = cleaned_url
            
            if is_valid and is_playlist:
                # Playlist-Modus (inklusive Mix)
                st.session_state.is_playlist_mode = True

                # Erkenne ob es ein Mix ist
                playlist_id_match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', cleaned_url)
                is_mix = False
                if playlist_id_match:
                    playlist_id = playlist_id_match.group(1)
                    is_mix = playlist_id.startswith('RD')

                # Auswahl: komplette Playlist/Mix vs. einzelne Songs
                st.markdown("### 📥 Download-Optionen")
                dl_mode = st.radio(
                    "Bitte wählen:",
                    options=["Komplette Playlist/Mix herunterladen", "Einzelne Songs auswählen"],
                    index=0,
                    horizontal=False
                )

                if dl_mode == "Komplette Playlist/Mix herunterladen":
                    # Hinweis und Direktladung
                    if is_mix:
                        st.info("🎵 Mix-URL erkannt - extrahiere bis zu {0} Songs...".format(MAX_MIX_SIZE))
                    else:
                        st.info("📋 Playlist-URL erkannt - lade bis zu {0} Videos...".format(MAX_PLAYLIST_SIZE))

                    if handle_playlist_url(cleaned_url) and st.session_state.playlist_videos:
                        # Standard: alle Elemente vorselektieren
                        st.session_state.selected_videos = list(range(len(st.session_state.playlist_videos)))

                        # Direkt-Download-Button für komplette Auswahl
                        total_items = len(st.session_state.playlist_videos)
                        content_type = "Songs" if is_mix else "Videos"
                        button_text = f"⬇️ Komplette {('Mix' if is_mix else 'Playlist')} als ZIP herunterladen ({total_items} {content_type})"

                        if st.button(button_text, type="primary", use_container_width=True, key="download_all_playlist"):
                            # Rate Limiting prüfen
                            rate_ok, rate_msg = check_rate_limit(client_ip, session_id)
                            if not rate_ok:
                                st.warning(f"🚫 {rate_msg}")
                                return

                            # Batch starten
                            st.session_state.batch_download_in_progress = True
                            update_download_tracking(client_ip, session_id)

                            videos_to_download = [
                                (item['url'], item['title'])
                                for item in st.session_state.playlist_videos
                            ]

                            # Progress-Anzeige
                            progress_bar = st.progress(0)
                            status_text = st.empty()
                            detail_text = st.empty()

                            def update_batch_progress(percent):
                                progress_bar.progress(percent)
                                download_type = "Mix-Download" if is_mix else "Playlist-Download"
                                status_text.text(f"{download_type} läuft... {percent}%")

                            def update_status(message):
                                detail_text.text(message)

                            status_text.text(f"Starte {('Mix' if is_mix else 'Playlist')}-Download...")

                            try:
                                downloaded_files, failed_downloads = download_multiple_videos(
                                    videos_to_download, update_batch_progress, update_status
                                )

                                if downloaded_files:
                                    status_text.text("Erstelle ZIP-Datei...")
                                    detail_text.text("Komprimiere heruntergeladene Dateien...")

                                    # ZIP-Dateiname anhand Playlisten-/Mixtitel wählen
                                    playlist_title = None
                                    try:
                                        if is_mix:
                                            mix_info = extract_mix_playlist_info(cleaned_url)
                                            if mix_info and mix_info.get('title'):
                                                playlist_title = mix_info['title']
                                        else:
                                            pl_info = extract_playlist_info(cleaned_url)
                                            if pl_info and pl_info.get('title'):
                                                playlist_title = pl_info['title']
                                    except Exception:
                                        playlist_title = None

                                    if not playlist_title:
                                        filename_prefix = "mix" if is_mix else "playlist"
                                        playlist_title = f"{filename_prefix}_download"

                                    zip_filename = clean_filename(f"{playlist_title}")
                                    zip_data = create_zip_file(downloaded_files, f"{zip_filename}.zip")

                                    if zip_data:
                                        progress_bar.progress(100)
                                        status_text.text("ZIP-Datei erstellt!")
                                        detail_text.text("Bereite Download vor...")

                                        st.markdown('<div class="download-section">', unsafe_allow_html=True)
                                        download_title = "🎵 Ihr Mix-Download ist bereit!" if is_mix else "📥 Ihr Playlist-Download ist bereit!"
                                        st.markdown(f"### {download_title}")

                                        download_script, error = create_zip_download_link(zip_data, f"{zip_filename}.zip")

                                        if download_script:
                                            st.components.v1.html(download_script, height=0)
                                            st.success("✅ Automatischer Download gestartet!")
                                            st.info("💡 Falls der Download nicht automatisch startet, verwenden Sie den Button unten:")
                                            _ = create_streamlit_download_button(zip_data, f"{zip_filename}.zip")
                                        else:
                                            st.warning(f"⚠️ Automatischer Download via JS-Snippet nicht möglich: {error}")
                                            st.info("Starte alternativen Auto-Download...")
                                            import base64 as _b64
                                            alt_b64 = _b64.b64encode(zip_data).decode()
                                            alt_name = f"{zip_filename}.zip"
                                            alt_js = f"""
                                            <script>
                                            (function() {{
                                                try {{
                                                    const link = document.createElement('a');
                                                    link.href = 'data:application/zip;base64,{alt_b64}';
                                                    link.download = '{alt_name}';
                                                    link.style.display = 'none';
                                                    document.body.appendChild(link);
                                                    link.click();
                                                    setTimeout(function() {{
                                                        try {{ document.body.removeChild(link); }} catch(e) {{}}
                                                    }}, 1000);
                                                }} catch (e) {{
                                                    console.error('Alt-Auto-Download Fehler:', e);
                                                    alert('Automatischer Download fehlgeschlagen. Bitte nutzen Sie den Button.');
                                                }}
                                            }})();
                                            </script>
                                            """
                                            st.components.v1.html(alt_js, height=0)
                                            _ = create_streamlit_download_button(zip_data, f"{zip_filename}.zip")

                                        st.markdown('</div>', unsafe_allow_html=True)

                                        success_count = len(downloaded_files)
                                        total_count = len(videos_to_download)
                                        content_info = f"📊 ZIP-Größe: {len(zip_data)/(1024*1024):.2f} MB | {content_type}: {success_count}"
                                        if is_mix:
                                            content_info += " | 🎵 Mix-Songs von YouTube generiert"
                                        st.info(content_info)

                                    else:
                                        st.error("❌ ZIP-Erstellung fehlgeschlagen")

                                    # Session zurücksetzen
                                    st.session_state.selected_videos = []
                                    st.session_state.playlist_videos = []
                                    st.session_state.clear_input = True
                                    st.session_state.input_key += 1
                                else:
                                    st.error("❌ Alle Downloads fehlgeschlagen")

                            except Exception as e:
                                st.error(f"❌ Batch-Download Fehler: {str(e)}")
                            finally:
                                st.session_state.batch_download_in_progress = False
                                try:
                                    progress_bar.empty()
                                    status_text.empty()
                                    detail_text.empty()
                                except:
                                    pass
                                gc.collect()

                else:
                    # Einzelne Songs auswählen
                    # Hinweistext passend zum Typ
                    if is_mix:
                        st.info("🎵 Mix-URL erkannt - extrahiere Songs zur Auswahl...")
                    else:
                        st.info("📋 Playlist-URL erkannt - lade Videos zur Auswahl...")

                    # Bestehenden Flow für Auswahl verwenden
                    if handle_playlist_url(cleaned_url):
                        if st.session_state.playlist_videos:
                            st.markdown("### 🎛️ Einzelne Titel auswählen:")

                            col_select1, col_select2, col_select3 = st.columns([1, 1, 2])
                            with col_select1:
                                if st.button("✅ Alle auswählen", use_container_width=True, key="pick_all"):
                                    st.session_state.selected_videos = list(range(len(st.session_state.playlist_videos)))
                                    st.rerun()

                            with col_select2:
                                if st.button("❌ Alle abwählen", use_container_width=True, key="pick_none"):
                                    st.session_state.selected_videos = []
                                    st.rerun()

                            with col_select3:
                                if st.session_state.selected_videos:
                                    content_type = "Songs" if is_mix else "Videos"
                                    st.write(f"🎵 {len(st.session_state.selected_videos)} {content_type} ausgewählt")

                            # Liste rendern (wie bisher)
                            for i, video in enumerate(st.session_state.playlist_videos):
                                col_checkbox, col_info = st.columns([1, 10])
                                with col_checkbox:
                                    is_selected = i in st.session_state.selected_videos
                                    if st.checkbox("Video auswählen", value=is_selected, key=f"video_pick_{i}", label_visibility="collapsed"):
                                        if i not in st.session_state.selected_videos:
                                            st.session_state.selected_videos.append(i)
                                    else:
                                        if i in st.session_state.selected_videos:
                                            st.session_state.selected_videos.remove(i)
                                with col_info:
                                    css_class = "selected-video" if is_selected else "video-item"
                                    track_number = f"Track {i+1}: " if is_mix else ""
                                    st.markdown(f"""
                                    <div class="{css_class}">
                                        <strong>{track_number}{video['title']}</strong><br>
                                        <small>⏱️ {format_duration(video['duration'])} | 👤 {video['uploader']}</small>
                                    </div>
                                    """, unsafe_allow_html=True)

                            # Download nur ausgewählte
                            if st.session_state.selected_videos and not st.session_state.batch_download_in_progress:
                                content_type = "Songs" if is_mix else "Videos"
                                button_text = f"🎵 {len(st.session_state.selected_videos)} ausgewählte {content_type} als ZIP herunterladen"

                                if st.button(button_text, type="primary", use_container_width=True, key="download_selected"):
                                    rate_ok, rate_msg = check_rate_limit(client_ip, session_id)
                                    if not rate_ok:
                                        st.warning(f"🚫 {rate_msg}")
                                        return

                                    st.session_state.batch_download_in_progress = True
                                    update_download_tracking(client_ip, session_id)

                                    videos_to_download = [
                                        (st.session_state.playlist_videos[i]['url'],
                                         st.session_state.playlist_videos[i]['title'])
                                        for i in st.session_state.selected_videos
                                    ]

                                    progress_bar = st.progress(0)
                                    status_text = st.empty()
                                    detail_text = st.empty()

                                    def update_batch_progress(percent):
                                        progress_bar.progress(percent)
                                        download_type = "Mix-Download" if is_mix else "Playlist-Download"
                                        status_text.text(f"{download_type} (Auswahl) läuft... {percent}%")

                                    def update_status(message):
                                        detail_text.text(message)

                                    status_text.text(f"Starte Download der Auswahl...")

                                    try:
                                        downloaded_files, failed_downloads = download_multiple_videos(
                                            videos_to_download, update_batch_progress, update_status
                                        )

                                        if downloaded_files:
                                            status_text.text("Erstelle ZIP-Datei...")
                                            detail_text.text("Komprimiere heruntergeladene Dateien...")

                                            # ZIP-Dateiname anhand Playlisten-/Mixtitel wählen
                                            playlist_title = None
                                            try:
                                                if is_mix:
                                                    mix_info = extract_mix_playlist_info(cleaned_url)
                                                    if mix_info and mix_info.get('title'):
                                                        playlist_title = f"{mix_info['title']} (Auswahl)"
                                                else:
                                                    pl_info = extract_playlist_info(cleaned_url)
                                                    if pl_info and pl_info.get('title'):
                                                        playlist_title = f"{pl_info['title']} (Auswahl)"
                                            except Exception:
                                                playlist_title = None

                                            if not playlist_title:
                                                filename_prefix = "mix_selection" if is_mix else "playlist_selection"
                                                playlist_title = filename_prefix

                                            zip_filename = clean_filename(f"{playlist_title}")
                                            zip_data = create_zip_file(downloaded_files, f"{zip_filename}.zip")

                                            if zip_data:
                                                progress_bar.progress(100)
                                                status_text.text("ZIP-Datei erstellt!")
                                                detail_text.text("Bereite Download vor...")

                                                st.markdown('<div class="download-section">', unsafe_allow_html=True)
                                                download_title = "🎵 Ihr Auswahl-Download ist bereit!"
                                                st.markdown(f"### {download_title}")

                                                download_script, error = create_zip_download_link(zip_data, f"{zip_filename}.zip")
                                                if download_script:
                                                    st.components.v1.html(download_script, height=0)
                                                    st.success("✅ Automatischer Download gestartet!")
                                                    st.info("💡 Falls der Download nicht automatisch startet, verwenden Sie den Button unten:")
                                                    _ = create_streamlit_download_button(zip_data, f"{zip_filename}.zip")
                                                else:
                                                    st.warning(f"⚠️ Automatischer Download via JS-Snippet nicht möglich: {error}")
                                                    _ = create_streamlit_download_button(zip_data, f"{zip_filename}.zip")

                                                st.markdown('</div>', unsafe_allow_html=True)
                                            else:
                                                st.error("❌ ZIP-Erstellung fehlgeschlagen")

                                            # Zurücksetzen nur Auswahl (Playlist behalten, falls weitere Auswahl gewünscht)
                                            st.session_state.selected_videos = []
                                        else:
                                            st.error("❌ Alle Downloads fehlgeschlagen")

                                    except Exception as e:
                                        st.error(f"❌ Batch-Download Fehler: {str(e)}")
                                    finally:
                                        st.session_state.batch_download_in_progress = False
                                        try:
                                            progress_bar.empty()
                                            status_text.empty()
                                            detail_text.empty()
                                        except:
                                            pass
                                        gc.collect()
                    else:
                        st.markdown("---")
                        suggest_alternative_playlists()
                        return
                        
            elif is_valid and not is_playlist:
                # Einzelvideo-Modus mit verbesserter Fehlerbehandlung
                st.session_state.is_playlist_mode = False
                video_id = extract_video_id(cleaned_url)
                
                if video_id and (video_id != st.session_state.last_video_id or st.session_state.download_finished) and not st.session_state.current_download:
                    # Rate Limiting prüfen
                    rate_ok, rate_msg = check_rate_limit(client_ip, session_id)
                    if not rate_ok:
                        st.warning(f"🚫 {rate_msg}")
                        return
                    
                    # Download-Tracking aktualisieren
                    update_download_tracking(client_ip, session_id)
                    
                    st.session_state.last_video_id = video_id
                    st.session_state.download_completed = False
                    st.session_state.current_download = True
                    st.session_state.auto_download_triggered = False
                    st.session_state.download_finished = False
                    st.session_state.file_saved = False
                    
                    # Video-Info mit Sicherheitschecks
                    info = get_video_info(cleaned_url)
                    
                    if not info:
                        st.session_state.current_download = False
                        st.session_state.download_finished = True
                        if st.session_state.active_downloads > 0:
                            st.session_state.active_downloads -= 1
                        st.error("❌ Video nicht verfügbar oder zu lang (max. 1 Stunde)")
                        return
                    
                    # Video-Details
                    col_info1, col_info2 = st.columns([1, 2])
                    with col_info1:
                        if info['thumbnail']:
                            st.image(info['thumbnail'], width=200)
                    
                    with col_info2:
                        st.write(f"**{info['title']}**")
                        st.write(f"**Kanal:** {info['uploader']}")
                        st.write(f"**Dauer:** {format_duration(info['duration'])}")
                        if info['view_count']:
                            st.write(f"**Aufrufe:** {info['view_count']:,}")
                    
                    st.markdown("---")
                    
                    # Progress
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    def update_progress(percent):
                        progress_bar.progress(percent)
                        if percent < 100:
                            status_text.text(f"Download läuft... {percent}%")
                        else:
                            status_text.text("Download abgeschlossen!")
                    
                    status_text.text("Download wird gestartet...")
                    
                    try:
                        file_path, result = download_audio_with_progress(cleaned_url, update_progress)
                        
                        if file_path and os.path.exists(file_path):
                            st.session_state.download_completed = True
                            st.session_state.download_count += 1
                            
                            filename = clean_filename(f"{info['title']}.mp3")
                            
                            status_text.text("Datei wird vorbereitet...")
                            
                            # Debug-Info
                            file_size = os.path.getsize(file_path) / (1024 * 1024)
                            print(f"Download erfolgreich: {file_path} ({file_size:.2f} MB)")
                            
                            if not st.session_state.auto_download_triggered:
                                download_script = create_download_link_and_clear_input(file_path, filename)
                                st.components.v1.html(download_script, height=0)
                                st.session_state.auto_download_triggered = True
                                st.session_state.file_saved = True
                            
                            try:
                                os.remove(file_path)
                                print(f"Temp-Datei gelöscht: {file_path}")
                            except Exception as e:
                                print(f"Warnung: Temp-Datei konnte nicht gelöscht werden: {e}")
                            
                            st.session_state.download_finished = True
                            st.session_state.current_download = False
                            
                            st.success("✅ Download erfolgreich!")
                            st.info(f"📊 Dateigröße: {file_size:.2f} MB")
                            
                            # Input-Feld leeren
                            st.session_state.clear_input = True
                            st.session_state.input_key += 1
                            st.session_state.cleaned_url = ""
                            
                            time.sleep(2)
                            st.rerun()
                            
                        else:
                            st.session_state.current_download = False
                            st.session_state.download_finished = True
                            
                            # Detaillierte Fehleranalyse
                            error_details = result if result else "Unbekannter Fehler"
                            st.error(f"❌ Download fehlgeschlagen: {error_details}")
                            
                            # Hilfreiche Tipps basierend auf dem Fehler
                            if "unavailable" in error_details.lower():
                                st.info("💡 **Tipp:** Video wurde möglicherweise entfernt oder ist privat")
                            elif "region" in error_details.lower():
                                st.info("💡 **Tipp:** Video ist in Ihrer Region gesperrt")
                            elif "age" in error_details.lower():
                                st.info("💡 **Tipp:** Altersverifizierung erforderlich")
                            elif "timeout" in error_details.lower():
                                st.info("💡 **Tipp:** Versuchen Sie es bei besserer Internetverbindung erneut")
                            else:
                                st.info("💡 **Tipp:** Überprüfen Sie die URL und versuchen Sie es erneut")
                            
                            # Debug-Button für detaillierte Diagnose
                            if st.button("🔧 Detaillierte Diagnose"):
                                issues = diagnose_download_issues()
                                if issues:
                                    st.error("System-Probleme gefunden:")
                                    for issue in issues:
                                        st.error(f"• {issue}")
                                else:
                                    st.info("System-Komponenten scheinen in Ordnung zu sein")
                            
                    except Exception as e:
                        st.session_state.current_download = False
                        st.session_state.download_finished = True
                        
                        error_msg = str(e)
                        st.error(f"❌ Kritischer Fehler: {error_msg}")
                        
                        # Debug-Informationen
                        with st.expander("🐛 Debug-Informationen"):
                            st.code(f"Fehler: {error_msg}")
                            st.code(f"URL: {cleaned_url}")
                            st.code(f"Video-ID: {video_id}")
                            
                            # System-Check
                            issues = diagnose_download_issues()
                            if issues:
                                st.write("**System-Probleme:**")
                                for issue in issues:
                                    st.error(f"• {issue}")
            
            elif not is_valid:
                st.error("🚫 Ungültige YouTube URL")
    
    with col2:
        # Rechte Spalte bewusst leer gelassen (seitliche Elemente entfernt)
        pass
    
    # Versteckter Footer-Werbeplatz
    st.markdown("---")
    st.components.v1.html("<!-- AD_FOOTER_PLACEHOLDER -->", height=0)

    # Abschnitt 'Lokale Videodatei in MP3 konvertieren' wurde gemäß Anforderung entfernt.

    # Analytics (versteckt)
    st.components.v1.html("""
    <script>
    // Google Analytics / Tracking Code hier einfügen
    // gtag('config', 'GA_MEASUREMENT_ID');
    
    function trackDownload(filename) {
        // Analytics Event
    }
    
    function trackPlaylistDownload(count) {
        // Playlist Download Tracking
    }
    
    function trackMixDownload(count) {
        // Mix Download Tracking
    }
    
    function trackAdClick(slot) {
        // Ad Click Tracking
    }
    
    function trackSpecialUrlConversion(type) {
        // Track special URL conversions
    }
    </script>
    """, height=0)

def run_server():
    """Starte den Server auf Port 8080"""
    print(f"🚀 Starting YouTube Audio Converter on http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    print(f"📱 Local access: http://localhost:{DEFAULT_PORT}")
    print(f"🌐 Network access: http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    print("🔄 Server starting...")

if __name__ == "__main__":
    # Server-Start-Meldungen
    run_server()
    
    # Prüfe ob Port als Argument übergeben wurde
    port = DEFAULT_PORT
    host = DEFAULT_HOST
    
    # Command Line Arguments verarbeiten
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            print(f"📍 Using custom port: {port}")
        except ValueError:
            print(f"⚠️  Invalid port argument, using default: {DEFAULT_PORT}")
            port = DEFAULT_PORT
    
    if len(sys.argv) > 2:
        host = sys.argv[2]
        print(f"📍 Using custom host: {host}")
    
    # Streamlit Konfiguration für Port 8080
    try:
        import subprocess
        import sys
        
        # Führe die App mit spezifischem Port aus
        if __name__ == "__main__":
            # Wenn direkt ausgeführt, starte Streamlit mit Port 8080
            cmd = [
                sys.executable, "-m", "streamlit", "run", __file__,
                "--server.port", str(port),
                "--server.address", host,
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
                "--server.enableCORS", "false",
                "--server.enableXsrfProtection", "false"
            ]
            
            print("🔧 Streamlit Configuration:")
            print(f"   - Host: {host}")
            print(f"   - Port: {port}")
            print(f"   - Headless: true")
            print(f"   - CORS: false")
            print("✅ Configuration applied")
            
            # Starte nur main() wenn über streamlit run aufgerufen
            main()
            
    except ImportError:
        # Fallback wenn subprocess nicht verfügbar
        main()