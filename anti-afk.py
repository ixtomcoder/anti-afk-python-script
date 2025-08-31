#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KeepAwake Auto (with top switches + robust FreeFileSync/RealTimeSync matching)
- Windows/macOS/Linux (focus: Windows 11).
- Switches at top: ALWAYS_ON, MOUSE_JIGGLE_ENABLED, MOUSE_JIGGLE_INTERVAL_SEC, MOUSE_JIGGLE_PIXELS, DEFAULT_WATCH, POLL_SEC.
- ALWAYS_ON=True => always keep awake (regardless of processes).
- Watch mode: detects target processes (case-insensitive, .exe ignored, substring match by default).
- Optional mouse jiggler (Windows) with configurable interval & pixel amplitude.
- CLI flags override the switches (see below).

New CLI extras:
  1) Matching modes: --match exact|startswith|substr|regex   (default: substr)
  2) Windows idle threshold for jiggler: --idle-threshold <sec>
  3) Display-only vs System-only: --display-only / --system-only
  4) Clean shutdown: signal handling (SIGINT/SIGTERM/SIGBREAK) and graceful stop

Examples:
  py anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2 --debug
  py anti-afk.py --watch "obs64,Audacity,REAPER,filesync" --match substr --jiggle --debug
  py anti-afk.py --duration 7200 --no-jiggle --debug
  py anti-afk.py --watch "filesync" --match startswith --display-only
"""

import argparse
import platform
import subprocess
import sys
import time
import threading
import shutil
import re
import signal
from datetime import datetime

# ---------- SWITCHES (top) ----------
ALWAYS_ON = False                  # True/False: always keep awake (ignores watch list)
MOUSE_JIGGLE_ENABLED = True       # True/False: enable mouse movement (Windows only)
MOUSE_JIGGLE_INTERVAL_SEC = 60     # Seconds: e.g., 50, 120, ...
MOUSE_JIGGLE_PIXELS = 1           # Pixel amplitude per mini-move (±n px)
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

# ---------- Shutdown handling ----------
SHUTDOWN = threading.Event()

def _on_signal(signum, frame):
    try:
        name = signal.Signals(signum).name  # Python 3.8+
    except Exception:
        name = str(signum)
    log("INFO", f"Signal received: {name} → initiating graceful shutdown.")
    SHUTDOWN.set()

def _install_signal_handlers():
    for s in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, s):
            try:
                signal.signal(getattr(signal, s), _on_signal)
            except Exception:
                pass

# ---------- Helpers ----------
def canon(name: str) -> str:
    n = name.strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n

def prepare_targets(raw_targets, mode: str):
    if mode == "regex":
        patterns = []
        for t in raw_targets:
            t = t.strip()
            if t:
                try:
                    patterns.append(re.compile(t, re.IGNORECASE))
                except re.error as e:
                    log("WARN", f"Ignoring invalid regex pattern '{t}': {e}")
        return patterns
    else:
        return [canon(t) for t in raw_targets if t.strip()]

def any_match(targets, running, mode: str):
    """
    Returns matched running process name or None.
    - running: set of canonicalized process names (lowercase, no .exe)
    - targets: list of canonical names (non-regex) or compiled regex patterns (regex mode)
    """
    if not targets or not running:
        return None
    if mode == "regex":
        for p in running:
            for pat in targets:
                if pat.search(p):
                    return p
        return None
    elif mode == "exact":
        tset = set(targets)
        for p in running:
            if p in tset:
                return p
        return None
    elif mode == "startswith":
        for p in running:
            for t in targets:
                if p.startswith(t):
                    return p
        return None
    else:  # "substr" (default)
        for p in running:
            for t in targets:
                if t in p:
                    return p
        return None

def any_match_bool_for_sample(targets, p: str, mode: str) -> bool:
    if mode == "regex":
        return any(pat.search(p) for pat in targets)
    elif mode == "exact":
        return p in targets
    elif mode == "startswith":
        return any(p.startswith(t) for t in targets)
    else:
        return any(t in p for t in targets)

# ---------- Windows: Mouse Jiggler (optional) ----------
class _WinMouseJiggler:
    """
    Small mouse 'jiggler' for Windows.
    Moves the mouse pointer every `interval_sec` seconds by `pixels` pixels to the right
    and immediately back again (±pixels). The cursor effectively stays in the same place.

    Parameters:
        interval_sec (int): interval in seconds (>=1).
        pixels (int): amplitude in pixels (>=1).
        idle_threshold_sec (int|None): only jiggle if user idle time >= threshold (seconds)

    Notes:
      - Does nothing on non-Windows systems (start/stop are no-ops).
      - Expects `OS` (platform.system()) and `log(level, msg)` to be present in module.
    """
    def __init__(self, interval_sec=50, pixels=1, idle_threshold_sec=None):
        self.interval = max(1, int(interval_sec))
        self.pixels = max(1, int(pixels))
        self.idle_threshold = None if idle_threshold_sec is None else max(0, int(idle_threshold_sec))
        self._stop = threading.Event()
        self._thr = None

    def _get_idle_seconds(self) -> float:
        # Windows: GetLastInputInfo
        import ctypes
        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        last_input_info = LASTINPUTINFO()
        last_input_info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input_info)):
            return 0.0
        tick_count = ctypes.windll.kernel32.GetTickCount()
        elapsed = tick_count - last_input_info.dwTime
        return float(elapsed) / 1000.0

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

        log("DEBUG", f"Mouse-jiggler thread started (interval={self.interval}s, pixels=±{self.pixels}, idle_threshold={self.idle_threshold}).")
        while not self._stop.is_set():
            try:
                if self.idle_threshold is not None:
                    idle = self._get_idle_seconds()
                    if idle < self.idle_threshold:
                        log("DEBUG", f"Mouse jiggler: idle {idle:.1f}s < threshold {self.idle_threshold}s → skip.")
                        # Check again soon while user is active
                        self._stop.wait(1.0)
                        continue
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
    def __init__(self, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, idle_threshold=None,
                 display_only=False, system_only=False):
        self.proc = None
        self._prev_state = None
        self.display_only = bool(display_only)
        self.system_only = bool(system_only)
        if jiggle_pixels is None:
            jiggle_pixels = MOUSE_JIGGLE_PIXELS
        self.jiggle = _WinMouseJiggler(interval_sec=jiggle_interval,
                                       pixels=jiggle_pixels,
                                       idle_threshold_sec=idle_threshold) if jiggle_enabled else None
        self._refresh_thr = None
        self._refresh_stop = threading.Event()
        self._es_flags = None  # Windows flags

    def _win_compute_flags(self):
        import ctypes
        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        if self.display_only and not self.system_only:
            return ES_CONTINUOUS | ES_DISPLAY_REQUIRED
        elif self.system_only and not self.display_only:
            return ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        else:
            return ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

    def _win_refresh_loop(self):
        import ctypes
        if self._es_flags is None:
            self._es_flags = self._win_compute_flags()
        log("DEBUG", "Windows refresh thread started.")
        while not self._refresh_stop.is_set():
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(self._es_flags)
                log("DEBUG", "SetThreadExecutionState refresh OK.")
            except Exception as e:
                log("WARN", f"SetThreadExecutionState refresh error: {e}")
            self._refresh_stop.wait(45)
        log("DEBUG", "Windows refresh thread stopped.")

    def __enter__(self):
        log("INFO", f"Enabling stay-awake ({OS}).")
        if OS == "Windows":
            import ctypes
            self._es_flags = self._win_compute_flags()
            self._prev_state = ctypes.windll.kernel32.SetThreadExecutionState(self._es_flags)
            if not self._prev_state:
                raise OSError("Initial SetThreadExecutionState failed.")
            log("DEBUG", f"Initial SetThreadExecutionState OK (prev={self._prev_state}, flags={hex(self._es_flags)}).")
            self._refresh_stop.clear()
            self._refresh_thr = threading.Thread(target=self._win_refresh_loop, daemon=True)
            self._refresh_thr.start()
            if self.jiggle:
                self.jiggle.start()
                log("INFO", "Mouse jiggler enabled.")
        elif OS == "Darwin":
            # caffeinate flags: -d (display), -i (idle/sleep). Use both if neither-only requested.
            args = ["caffeinate"]
            if self.display_only and not self.system_only:
                args += ["-d"]
            elif self.system_only and not self.display_only:
                args += ["-i"]
            else:
                args += ["-di"]
            self.proc = subprocess.Popen(args)
            log("DEBUG", f"macOS caffeinate started (PID {self.proc.pid}) with args: {' '.join(args)}.")
        elif OS == "Linux":
            # Prefer systemd-inhibit, then gnome-session-inhibit, then xdg-screensaver (display-only).
            if shutil.which("systemd-inhibit"):
                if self.display_only and not self.system_only:
                    what = "idle"
                elif self.system_only and not self.display_only:
                    what = "sleep"
                else:
                    what = "idle:sleep"
                self.proc = subprocess.Popen([
                    "systemd-inhibit", f"--what={what}",
                    "--mode=block", "--why=KeepAwake",
                    "bash", "-lc", "sleep infinity"
                ])
                log("DEBUG", f"systemd-inhibit started (PID {self.proc.pid}) what={what}.")
            elif shutil.which("gnome-session-inhibit"):
                args = ["gnome-session-inhibit", "--reason", "KeepAwake"]
                if self.display_only and not self.system_only:
                    args += ["--inhibit", "idle"]
                elif self.system_only and not self.display_only:
                    args += ["--inhibit", "suspend"]
                else:
                    args += ["--inhibit", "idle", "--inhibit", "suspend"]
                args += ["bash", "-lc", "sleep infinity"]
                self.proc = subprocess.Popen(args)
                log("DEBUG", f"gnome-session-inhibit started (PID {self.proc.pid}).")
            elif shutil.which("xdg-screensaver"):
                if self.system_only and not self.display_only:
                    raise EnvironmentError("No suitable system-only inhibitor found (xdg-screensaver handles display only).")
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
def watch_and_keep_awake(targets, match_mode="substr",
                         jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, idle_threshold=None,
                         poll=5, display_only=False, system_only=False):
    prepared = prepare_targets(targets if targets else DEFAULT_WATCH, match_mode)
    log("INFO", f"Watching processes (mode={match_mode}): " +
        (", ".join(targets) if targets else ", ".join(DEFAULT_WATCH)))
    active = False
    keeper = None
    try:
        while not SHUTDOWN.is_set():
            running = list_process_names()
            if current_level <= LEVELS["DEBUG"]:
                sample = sorted([p for p in running if any_match_bool_for_sample(prepared, p, match_mode)])[:10]
                if sample:
                    log("DEBUG", "Seen relevant processes: " + ", ".join(sample))
                else:
                    log("DEBUG", f"Active processes counted: {len(running)} (no matches).")

            hit = any_match(prepared, running, match_mode)

            if hit and not active:
                log("INFO", f"Target process detected → enabling stay-awake (hit: {hit}).")
                keeper = StayAwake(jiggle_enabled=jiggle_enabled,
                                   jiggle_interval=jiggle_interval,
                                   jiggle_pixels=jiggle_pixels,
                                   idle_threshold=idle_threshold,
                                   display_only=display_only,
                                   system_only=system_only)
                keeper.__enter__()  # manual enter because start/stop are dynamic
                active = True
            elif not hit and active:
                log("INFO", "No target process anymore → disabling stay-awake.")
                if keeper:
                    keeper.__exit__(None, None, None)
                    keeper = None
                active = False

            SHUTDOWN.wait(max(1, poll))
    except KeyboardInterrupt:
        log("INFO", "Interrupted (Ctrl+C).")
    finally:
        if keeper:
            keeper.__exit__(None, None, None)
        log("INFO", "Stopped.")

def keep_awake_for(duration, jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, idle_threshold=None,
                   display_only=False, system_only=False):
    log("INFO", f"Keeping awake for {duration} seconds. Ctrl+C to stop.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels,
                       idle_threshold=idle_threshold,
                       display_only=display_only,
                       system_only=system_only):
            SHUTDOWN.wait(max(0, int(duration)))
    except KeyboardInterrupt:
        log("INFO", "Interrupted (Ctrl+C).")
    log("INFO", "Done — power/save behavior restored.")

def keep_awake_always(jiggle_enabled=False, jiggle_interval=50, jiggle_pixels=None, idle_threshold=None,
                      display_only=False, system_only=False):
    log("INFO", "ALWAYS_ON active — keeping awake indefinitely. Ctrl+C to stop.")
    try:
        with StayAwake(jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels,
                       idle_threshold=idle_threshold,
                       display_only=display_only,
                       system_only=system_only):
            while not SHUTDOWN.is_set():
                SHUTDOWN.wait(3600)
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
    ap.add_argument("--idle-threshold", type=int, default=None, help="Only jiggle if user idle time ≥ N seconds (Windows).")

    ap.add_argument("--watch", type=str,
                    help="Comma-separated process names (e.g. 'obs64,Audacity,filesync'). "
                         "If omitted the default list is watched.")

    ap.add_argument("--match", type=str, choices=["exact", "startswith", "substr", "regex"],
                    default="substr", help="Process match mode (default: substr).")

    ap.add_argument("--duration", type=int,
                    help="Instead of watch: fixed duration in seconds to keep awake.")
    ap.add_argument("--poll", type=int, default=None, help="Interval (sec.) for process checks.")
    ap.add_argument("--debug", action="store_true", help="Show verbose debug logs.")

    # Display/System selection (mutually exclusive allowed as 'both' when none specified)
    ap.add_argument("--display-only", action="store_true", help="Prevent display sleep only.")
    ap.add_argument("--system-only", action="store_true", help="Prevent system sleep only.")

    args = ap.parse_args()

    _install_signal_handlers()
    set_debug(args.debug)

    if args.display_only and args.system_only:
        log("ERROR", "Choose at most one: --display-only OR --system-only.")
        sys.exit(2)

    # Effective settings from switches at top + optional CLI overrides
    always_on = ALWAYS_ON if args.always_on is None else args.always_on
    jiggle_enabled = MOUSE_JIGGLE_ENABLED if args.jiggle is None else args.jiggle
    jiggle_interval = MOUSE_JIGGLE_INTERVAL_SEC if args.jiggle_interval is None else args.jiggle_interval
    jiggle_pixels = MOUSE_JIGGLE_PIXELS if args.jiggle_pixels is None else args.jiggle_pixels
    poll = POLL_SEC if args.poll is None else args.poll
    idle_threshold = args.idle_threshold
    match_mode = args.match
    display_only = bool(args.display_only)
    system_only = bool(args.system_only)

    if args.duration and (args.watch or always_on):
        log("ERROR", "Please choose ONE mode: --duration OR --watch/default-list OR --always-on.")
        sys.exit(2)

    # Modes
    if args.duration:
        keep_awake_for(args.duration,
                       jiggle_enabled=jiggle_enabled,
                       jiggle_interval=jiggle_interval,
                       jiggle_pixels=jiggle_pixels,
                       idle_threshold=idle_threshold,
                       display_only=display_only,
                       system_only=system_only)
        return

    if always_on:
        keep_awake_always(jiggle_enabled=jiggle_enabled,
                          jiggle_interval=jiggle_interval,
                          jiggle_pixels=jiggle_pixels,
                          idle_threshold=idle_threshold,
                          display_only=display_only,
                          system_only=system_only)
        return

    targets = []
    if args.watch is not None:
        targets = [t.strip() for t in args.watch.split(",")]

    watch_and_keep_awake(targets,
                         match_mode=match_mode,
                         jiggle_enabled=jiggle_enabled,
                         jiggle_interval=jiggle_interval,
                         jiggle_pixels=jiggle_pixels,
                         idle_threshold=idle_threshold,
                         poll=poll,
                         display_only=display_only,
                         system_only=system_only)

if __name__ == "__main__":
    main()
