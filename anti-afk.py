#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KeepAwake Auto (mit Schaltern oben + robustem FreeFileSync/RealTimeSync-Match)
- Windows/macOS/Linux (Fokus: Windows 11).
- "Schalter" oben: ALWAYS_ON, MOUSE_JIGGLE_ENABLED, MOUSE_JIGGLE_INTERVAL_SEC, MOUSE_JIGGLE_PIXELS, DEFAULT_WATCH, POLL_SEC.
- ALWAYS_ON=True => hält immer wach (unabhängig von Prozessen).
- Watch-Modus: erkennt Zielprozesse (case-insensitive, .exe egal, Substring-Match).
- Optionaler Maus-Jiggle (Windows) mit konfigurierbarem Intervall & Pixel-Amplitude.
- CLI-Flags überschreiben die Schalter (siehe unten).

Beispiele:
  py anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2 --debug
  py anti-afk.py --watch "obs64,Audacity,REAPER,filesync" --jiggle --debug
  py anti-afk.py --duration 7200 --no-jiggle --debug
"""

import argparse
import platform
import subprocess
import sys
import time
import threading
import shutil
from datetime import datetime

# ---------- SCHALTER (oben) ----------
ALWAYS_ON = False                  # True/False: immer wach halten (ignoriert Watch-Liste)
MOUSE_JIGGLE_ENABLED = True       # True/False: Mausbewegung aktivieren (nur Windows)
MOUSE_JIGGLE_INTERVAL_SEC = 1     # Sekunden: z. B. 50, 120, ...
MOUSE_JIGGLE_PIXELS = 1          # Pixel-Amplitude pro Mini-Move (±n px)
POLL_SEC = 5                      # Prozess-Scan-Intervall in Sekunden

# Standard-Watchliste (case-insensitive, Substring-Match, .exe egal):
DEFAULT_WATCH = [
    "obs", "obs64", "audacity", "reaper",
    "ableton", "logic", "cubase",
    "fl", "fl64", "protools", "studio one",
    # FreeFileSync / RealTimeSync robust:
    "filesync", "freefilesync", "realtimesync"
]

# ---------- System ----------
OS = platform.system()

# ---------- Logging ----------
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
current_level = LEVELS["INFO"]

def log(level, msg):
    if LEVELS[level] >= current_level:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{level}] {msg}", flush=True)

def set_debug(enabled: bool):
    global current_level
    current_level = LEVELS["DEBUG"] if enabled else LEVELS["INFO"]

# ---------- Hilfsfunktionen ----------
def canon(name: str) -> str:
    n = name.strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n

def any_match(targets, running):
    # substring-Match: "filesync" trifft "freefilesync", etc.
    for t in targets:
        for p in running:
            if t in p:
                return p  # gib den gefundenen Namen zurück
    return None

# ---------- Windows: Maus-Jiggle (optional) ----------
class _WinMouseJiggler:
    """
    Kleiner Maus-'Jiggler' für Windows.
    Bewegt den Mauszeiger alle `interval_sec` Sekunden um `pixels` Pixel nach rechts
    und direkt wieder zurück (±pixels). Effektiv bleibt der Cursor am selben Ort.

    Parameter:
        interval_sec (int): Intervall in Sekunden (>=1).
        pixels (int): Amplitude in Pixeln (>=1).

    Hinweise:
      - Tut nichts auf Nicht-Windows-Systemen (Start/Stop sind No-Ops).
      - Erwartet, dass `OS` (platform.system()) und `log(level, msg)` im Modul vorhanden sind.
    """
    def __init__(self, interval_sec=50, pixels=1):
        self.interval = max(1, int(interval_sec))
        self.pixels = max(1, int(pixels))
        self._stop = threading.Event()
        self._thr = None

    def _run(self):
        import ctypes
        from ctypes import wintypes
        MOUSEEVENTF_MOVE = 0x0001

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = (("dx", wintypes.LONG),
                        ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p))

        class INPUT(ctypes.Structure):
            class _I(ctypes.Union):
                _fields_ = (("mi", MOUSEINPUT),)
            _anonymous_ = ("i",)
            _fields_ = (("type", wintypes.DWORD), ("i", _I))

        SendInput = ctypes.windll.user32.SendInput

        def move(dx, dy):
            inp = INPUT(type=0)  # 0 = INPUT_MOUSE
            inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, None)
            SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

        log("DEBUG", f"Maus-Jiggle Thread gestartet (Intervall={self.interval}s, Pixels=±{self.pixels}).")
        while not self._stop.is_set():
            try:
                move(self.pixels, 0)
                move(-self.pixels, 0)
                log("DEBUG", "Maus-Jiggle: mini move.")
            except Exception as e:
                log("WARN", f"Maus-Jiggle Fehler: {e}")
            self._stop.wait(self.interval)
        log("DEBUG", "Maus-Jiggle Thread beendet.")

    def start(self):
        if OS != "Windows":
            return
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        if OS != "Windows":
            return
        if self._thr:
            self._stop.set()
            self._thr.join(timeout=2)


# ---------- StayAwake Controller ----------
class StayAwake:
    """Hält das System wach; unter Windows periodischer Refresh von ExecutionState."""
    def __init__(self, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None):
        self.proc = None
        self._prev_state = None
        if jiggle_pixels is None:
            jiggle_pixels = MOUSE_JIGGLE_PIXELS
        self.jiggle = _WinMouseJiggler(interval_sec=jiggle_interval, pixels=jiggle_pixels) if jiggle_enabled else None
        self._refresh_thr = None
        self._refresh_stop = threading.Event()

    def _win_refresh_loop(self):
        import ctypes
        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        log("DEBUG", "Windows-Refresh-Thread gestartet.")
        while not self._refresh_stop.is_set():
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
                log("DEBUG", "SetThreadExecutionState refresh OK.")
            except Exception as e:
                log("WARN", f"SetThreadExecutionState refresh Fehler: {e}")
            self._refresh_stop.wait(45)
        log("DEBUG", "Windows-Refresh-Thread beendet.")

    def __enter__(self):
        log("INFO", f"Aktiviere Wachhalten ({OS}).")
        if OS == "Windows":
            import ctypes
            ES_CONTINUOUS       = 0x80000000
            ES_SYSTEM_REQUIRED  = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            self._prev_state = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            if not self._prev_state:
                raise OSError("SetThreadExecutionState initial fehlgeschlagen.")
            log("DEBUG", f"Initialer SetThreadExecutionState OK (prev={self._prev_state}).")
            self._refresh_stop.clear()
            self._refresh_thr = threading.Thread(target=self._win_refresh_loop, daemon=True)
            self._refresh_thr.start()
            if self.jiggle:
                self.jiggle.start()
                log("INFO", "Maus-Jiggle (Fallback) aktiviert.")
        elif OS == "Darwin":
            self.proc = subprocess.Popen(["caffeinate", "-di"])
            log("DEBUG", f"macOS caffeinate gestartet (PID {self.proc.pid}).")
        elif OS == "Linux":
            if shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen([
                    "systemd-inhibit", "--what=idle:sleep",
                    "--mode=block", "--why=KeepAwake Recording",
                    "bash", "-lc", "sleep infinity"
                ])
                log("DEBUG", f"systemd-inhibit gestartet (PID {self.proc.pid}).")
            elif shutil.which("gnome-session-inhibit"):
                self.proc = subprocess.Popen([
                    "gnome-session-inhibit",
                    "--inhibit", "idle", "--inhibit", "suspend",
                    "--reason", "KeepAwake Recording",
                    "bash", "-lc", "sleep infinity"
                ])
                log("DEBUG", f"gnome-session-inhibit gestartet (PID {self.proc.pid}).")
            elif shutil.which("xdg-screensaver"):
                self.proc = subprocess.Popen(["bash", "-lc",
                    "while true; do xdg-screensaver reset; sleep 50; done"])
                log("DEBUG", f"xdg-screensaver Reset-Loop gestartet (PID {self.proc.pid}).")
            else:
                raise EnvironmentError("Kein Inhibitor gefunden (systemd/gnome/xdg).")
        else:
            raise NotImplementedError(f"Nicht unterstütztes OS: {OS}")
        return self

    def __exit__(self, exc_type, exc, tb):
        log("INFO", f"Deaktiviere Wachhalten ({OS}).")
        try:
            if OS == "Windows":
                import ctypes
                ES_CONTINUOUS = 0x80000000
                if self._refresh_thr:
                    self._refresh_stop.set()
                    self._refresh_thr.join(timeout=2)
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                if self.jiggle:
                    self.jiggle.stop()
                    log("DEBUG", "Maus-Jiggle deaktiviert.")
            elif self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            return False

# ---------- Prozesse lesen ----------
def list_process_names():
    names = set()
    try:
        if OS == "Windows":
            out = subprocess.check_output(["tasklist", "/fo", "csv", "/nh"], text=True, errors="ignore")
            import csv, io
            for row in csv.reader(io.StringIO(out)):
                if row:
                    names.add(canon(row[0]))  # Bildname (exe) -> "name"
        else:
            out = subprocess.check_output(["ps", "-A", "-o", "comm="], text=True, errors="ignore")
            for line in out.splitlines():
                nm = canon(line.split("/")[-1])
                if nm:
                    names.add(nm)
    except Exception as e:
        log("WARN", f"Prozessliste konnte nicht gelesen werden: {e}")
    return names

# ---------- Modi ----------
def watch_and_keep_awake(targets, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, poll=5):
    targets = [canon(t) for t in targets if t.strip()] or [canon(t) for t in DEFAULT_WATCH]
    log("INFO", "Überwache Prozesse: " + ", ".join(targets))
    active = False
    keeper = None
    try:
        while True:
            running = list_process_names()
            if current_level <= LEVELS["DEBUG"]:
                sample = sorted([p for p in running if any(t in p for t in targets)])[:10]
                if sample:
                    log("DEBUG", "Gesehene relevante Prozesse: " + ", ".join(sample))
                else:
                    log("DEBUG", f"Aktive Prozesse gezählt: {len(running)} (keine Matches).")

            hit = any_match(targets, running)

            if hit and not active:
                log("INFO", f"Zielprozess erkannt → Wachhalten EIN (Treffer: {hit}).")
                keeper = StayAwake(jiggle_enabled=jiggle_enabled,
                                   jiggle_interval=jiggle_interval,
                                   jiggle_pixels=jiggle_pixels)
                keeper.__enter__()  # Kontext manuell, weil wir Start/Stop dynamisch wollen
                active = True
            elif not hit and active:
                log("INFO", "Kein Zielprozess mehr → Wachhalten AUS.")
                if keeper:
                    keeper.__exit__(None, None, None)
                    keeper = None
                active = False

            time.sleep(max(1, poll))
    except KeyboardInterrupt:
        log("INFO", "Abbruch (Strg+C).")
    finally:
        if keeper:
            keeper.__exit__(None, None, None)
        log("INFO", "Beendet.")

def keep_awake_for(duration, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None):
    log("INFO", f"Wachhalten für {duration} Sekunden. Strg+C zum Beenden.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels):
            time.sleep(duration)
    except KeyboardInterrupt:
        log("INFO", "Abbruch (Strg+C).")
    log("INFO", "Fertig – Energiesparverhalten wieder normal.")

def keep_awake_always(jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None):
    log("INFO", "ALWAYS_ON aktiv – hält dauerhaft wach. Strg+C zum Beenden.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels):
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        log("INFO", "Abbruch (Strg+C).")
    log("INFO", "Fertig – Energiesparverhalten wieder normal.")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Wachhalten (mit Schaltern + Auto On/Off) und Debug-Logs.")
    # Booleans mit Paar-Flags (kompatibel):
    ap.add_argument("--always-on", dest="always_on", action="store_true", help="Immer wach halten.")
    ap.add_argument("--no-always-on", dest="always_on", action="store_false", help="Immer wach halten AUS.")
    ap.set_defaults(always_on=None)  # None => benutze Schalter oben

    ap.add_argument("--jiggle", dest="jiggle", action="store_true", help="Maus-Jiggle aktivieren (Windows).")
    ap.add_argument("--no-jiggle", dest="jiggle", action="store_false", help="Maus-Jiggle deaktivieren.")
    ap.set_defaults(jiggle=None)

    ap.add_argument("--jiggle-interval", type=int, default=None, help="Intervall für Maus-Jiggle in Sekunden.")
    ap.add_argument("--jiggle-pixels", type=int, default=None, help="Pixel-Amplitude pro Mini-Move (±n px).")
    ap.add_argument("--watch", type=str,
                    help="Kommagetrennte Prozessnamen (z. B. 'obs64,Audacity,filesync'). "
                         "Ohne Angabe wird die Standardliste überwacht.")
    ap.add_argument("--duration", type=int,
                    help="Statt Watch: feste Dauer in Sekunden wach halten.")
    ap.add_argument("--poll", type=int, default=None, help="Intervall (Sek.) für Prozesscheck.")
    ap.add_argument("--debug", action="store_true", help="Ausführliche Debug-Logs anzeigen.")
    args = ap.parse_args()

    set_debug(args.debug)

    # Effektive Settings aus Schaltern oben + ggf. CLI-Override
    always_on = ALWAYS_ON if args.always_on is None else args.always_on
    jiggle_enabled = MOUSE_JIGGLE_ENABLED if args.jiggle is None else args.jiggle
    jiggle_interval = MOUSE_JIGGLE_INTERVAL_SEC if args.jiggle_interval is None else args.jiggle_interval
    jiggle_pixels = MOUSE_JIGGLE_PIXELS if args.jiggle_pixels is None else args.jiggle_pixels
    poll = POLL_SEC if args.poll is None else args.poll

    if args.duration and (args.watch or always_on):
        log("ERROR", "Bitte EINE Betriebsart wählen: --duration ODER --watch/Standardliste ODER --always-on.")
        sys.exit(2)

    # Betriebsarten
    if args.duration:
        keep_awake_for(args.duration,
                       jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels)
        return

    if always_on:
        keep_awake_always(jiggle_enabled=jiggle_enabled,
                          jiggle_interval=jiggle_interval,
                          jiggle_pixels=jiggle_pixels)
        return

    targets = []
    if args.watch is not None:
        targets = [t.strip() for t in args.watch.split(",")]
    watch_and_keep_awake(targets,
                         jiggle_enabled=jiggle_enabled,
                         jiggle_interval=jiggle_interval,
                         jiggle_pixels=jiggle_pixels,
                         poll=poll)

if __name__ == "__main__":
    main()
