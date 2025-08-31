#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KeepAwake Auto (with switches at top + robust FreeFileSync/RealTimeSync matching)
- Windows/macOS/Linux (focus: Windows 11).
- "Switches" at top: ALWAYS_ON, MOUSE_JIGGLE_ENABLED, MOUSE_JIGGLE_INTERVAL_SEC, MOUSE_JIGGLE_PIXELS, DEFAULT_WATCH, POLL_SEC.
- ALWAYS_ON=True => always keep awake (regardless of processes).
- Watch mode: detects target processes (case-insensitive, .exe ignored, substring match).
- Optional mouse jiggler (Windows) with configurable interval & pixel amplitude.
- CLI flags override the switches (see below).

Examples:
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

# ---------- SWITCHES (top) ----------
ALWAYS_ON = False                  # True/False: always keep awake (ignores watch list)
MOUSE_JIGGLE_ENABLED = True       # True/False: enable mouse movement (Windows only)
MOUSE_JIGGLE_INTERVAL_SEC = 1     # Seconds: e.g., 50, 120, ...
MOUSE_JIGGLE_PIXELS = 1          # Pixel amplitude per mini-move (±n px)
POLL_SEC = 5                      # Process-scan interval in seconds

# Default watch list (case-insensitive, substring match, .exe ignored):
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

# ---------- Helpers ----------
def canon(name: str) -> str:
    n = name.strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n

def any_match(targets, running):
    # substring match: "filesync" hits "freefilesync", etc.
    for t in targets:
        for p in running:
            if t in p:
                return p  # return the matched name
    return None

# ---------- Windows: Mouse Jiggler (optional) ----------
class _WinMouseJiggler:
    """
    Small mouse 'jiggler' for Windows.
    Moves the mouse pointer every `interval_sec` seconds by `pixels` pixels to the right
    and immediately back again (±pixels). The cursor effectively stays in the same place.

    Parameters:
        interval_sec (int): interval in seconds (>=1).
        pixels (int): amplitude in pixels (>=1).

    Notes:
      - Does nothing on non-Windows systems (start/stop are no-ops).
      - Expects `OS` (platform.system()) and `log(level, msg)` to be present in module.
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

        log("DEBUG", f"Mouse-jiggler thread started (interval={self.interval}s, pixels=±{self.pixels}).")
        while not self._stop.is_set():
            try:
                move(self.pixels, 0)
                move(-self.pixels, 0)
                log("DEBUG", "Mouse jiggler: mini move.")
            except Exception as e:
                log("WARN", f"Mouse jiggler error: {e}")
            self._stop.wait(self.interval)
        log("DEBUG", "Mouse-jiggler thread stopped.")

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
    """Keeps the system awake; on Windows it performs periodic SetThreadExecutionState refreshes."""
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
        log("DEBUG", "Windows refresh thread started.")
        while not self._refresh_stop.is_set():
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
                log("DEBUG", "SetThreadExecutionState refresh OK.")
            except Exception as e:
                log("WARN", f"SetThreadExecutionState refresh error: {e}")
            self._refresh_stop.wait(45)
        log("DEBUG", "Windows refresh thread stopped.")

    def __enter__(self):
        log("INFO", f"Enabling stay-awake ({OS}).")
        if OS == "Windows":
            import ctypes
            ES_CONTINUOUS       = 0x80000000
            ES_SYSTEM_REQUIRED  = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            self._prev_state = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            if not self._prev_state:
                raise OSError("Initial SetThreadExecutionState failed.")
            log("DEBUG", f"Initial SetThreadExecutionState OK (prev={self._prev_state}).")
            self._refresh_stop.clear()
            self._refresh_thr = threading.Thread(target=self._win_refresh_loop, daemon=True)
            self._refresh_thr.start()
            if self.jiggle:
                self.jiggle.start()
                log("INFO", "Mouse jiggler enabled.")
        elif OS == "Darwin":
            self.proc = subprocess.Popen(["caffeinate", "-di"])
            log("DEBUG", f"macOS caffeinate started (PID {self.proc.pid}).")
        elif OS == "Linux":
            if shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen([
                    "systemd-inhibit", "--what=idle:sleep",
                    "--mode=block", "--why=KeepAwake Recording",
                    "bash", "-lc", "sleep infinity"
                ])
                log("DEBUG", f"systemd-inhibit started (PID {self.proc.pid}).")
            elif shutil.which("gnome-session-inhibit"):
                self.proc = subprocess.Popen([
                    "gnome-session-inhibit",
                    "--inhibit", "idle", "--inhibit", "suspend",
                    "--reason", "KeepAwake Recording",
                    "bash", "-lc", "sleep infinity"
                ])
                log("DEBUG", f"gnome-session-inhibit started (PID {self.proc.pid}).")
            elif shutil.which("xdg-screensaver"):
                self.proc = subprocess.Popen(["bash", "-lc",
                    "while true; do xdg-screensaver reset; sleep 50; done"])
                log("DEBUG", f"xdg-screensaver reset loop started (PID {self.proc.pid}).")
            else:
                raise EnvironmentError("No inhibitor found (systemd/gnome/xdg).")
        else:
            raise NotImplementedError(f"Unsupported OS: {OS}")
        return self

    def __exit__(self, exc_type, exc, tb):
        log("INFO", f"Disabling stay-awake ({OS}).")
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
                    log("DEBUG", "Mouse jiggler stopped.")
            elif self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            return False

# ---------- Read processes ----------
def list_process_names():
    names = set()
    try:
        if OS == "Windows":
            out = subprocess.check_output(["tasklist", "/fo", "csv", "/nh"], text=True, errors="ignore")
            import csv, io
            for row in csv.reader(io.StringIO(out)):
                if row:
                    names.add(canon(row[0]))  # image name (exe) -> "name"
        else:
            out = subprocess.check_output(["ps", "-A", "-o", "comm="], text=True, errors="ignore")
            for line in out.splitlines():
                nm = canon(line.split("/")[-1])
                if nm:
                    names.add(nm)
    except Exception as e:
        log("WARN", f"Could not read process list: {e}")
    return names

# ---------- Modes ----------
def watch_and_keep_awake(targets, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, poll=5):
    targets = [canon(t) for t in targets if t.strip()] or [canon(t) for t in DEFAULT_WATCH]
    log("INFO", "Watching processes: " + ", ".join(targets))
    active = False
    keeper = None
    try:
        while True:
            running = list_process_names()
            if current_level <= LEVELS["DEBUG"]:
                sample = sorted([p for p in running if any(t in p for t in targets)])[:10]
                if sample:
                    log("DEBUG", "Seen relevant processes: " + ", ".join(sample))
                else:
                    log("DEBUG", f"Active processes counted: {len(running)} (no matches).")

            hit = any_match(targets, running)

            if hit and not active:
                log("INFO", f"Target process detected → enabling stay-awake (hit: {hit}).")
                keeper = StayAwake(jiggle_enabled=jiggle_enabled,
                                   jiggle_interval=jiggle_interval,
                                   jiggle_pixels=jiggle_pixels)
                keeper.__enter__()  # enter manually because start/stop are dynamic
                active = True
            elif not hit and active:
                log("INFO", "No target process anymore → disabling stay-awake.")
                if keeper:
                    keeper.__exit__(None, None, None)
                    keeper = None
                active = False

            time.sleep(max(1, poll))
    except KeyboardInterrupt:
        log("INFO", "Interrupted (Ctrl+C).")
    finally:
        if keeper:
            keeper.__exit__(None, None, None)
        log("INFO", "Stopped.")

def keep_awake_for(duration, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None):
    log("INFO", f"Keeping awake for {duration} seconds. Ctrl+C to stop.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels):
            time.sleep(duration)
    except KeyboardInterrupt:
        log("INFO", "Interrupted (Ctrl+C).")
    log("INFO", "Done — power/save behavior restored.")

def keep_awake_always(jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None):
    log("INFO", "ALWAYS_ON active — keeping awake indefinitely. Ctrl+C to stop.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels):
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        log("INFO", "Interrupted (Ctrl+C).")
    log("INFO", "Done — power/save behavior restored.")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Keep-awake (with switches + auto on/off) and debug logs.")
    # Booleans with paired flags (compatible):
    ap.add_argument("--always-on", dest="always_on", action="store_true", help="Always keep awake.")
    ap.add_argument("--no-always-on", dest="always_on", action="store_false", help="Disable always-on.")
    ap.set_defaults(always_on=None)  # None => use switch at top

    ap.add_argument("--jiggle", dest="jiggle", action="store_true", help="Enable mouse jiggler (Windows).")
    ap.add_argument("--no-jiggle", dest="jiggle", action="store_false", help="Disable mouse jiggler.")
    ap.set_defaults(jiggle=None)

    ap.add_argument("--jiggle-interval", type=int, default=None, help="Interval for mouse jiggler in seconds.")
    ap.add_argument("--jiggle-pixels", type=int, default=None, help="Pixel amplitude per mini-move (±n px).")
    ap.add_argument("--watch", type=str,
                    help="Comma-separated process names (e.g. 'obs64,Audacity,filesync'). "
                         "If omitted the default list is watched.")
    ap.add_argument("--duration", type=int,
                    help="Instead of watch: fixed duration in seconds to keep awake.")
    ap.add_argument("--poll", type=int, default=None, help="Interval (sec.) for process checks.")
    ap.add_argument("--debug", action="store_true", help="Show verbose debug logs.")
    args = ap.parse_args()

    set_debug(args.debug)

    # Effective settings from switches at top + optional CLI overrides
    always_on = ALWAYS_ON if args.always_on is None else args.always_on
    jiggle_enabled = MOUSE_JIGGLE_ENABLED if args.jiggle is None else args.jiggle
    jiggle_interval = MOUSE_JIGGLE_INTERVAL_SEC if args.jiggle_interval is None else args.jiggle_interval
    jiggle_pixels = MOUSE_JIGGLE_PIXELS if args.jiggle_pixels is None else args.jiggle_pixels
    poll = POLL_SEC if args.poll is None else args.poll

    if args.duration and (args.watch or always_on):
        log("ERROR", "Please choose ONE mode: --duration OR --watch/default-list OR --always-on.")
        sys.exit(2)

    # Modes
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
