# auto-keep-awake-prevent-sleep-python

> Keep your computer awake on **Windows / macOS / Linux**. Prevent sleep/idle using OS-native inhibitors, with an optional **Windows mouse jiggler** (configurable interval & pixel amplitude). Can auto-enable while certain apps (e.g. **FreeFileSync / RealTimeSync / OBS**) are running.

![OS](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-informational)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-success)

---

## Features
- **ALWAYS_ON** switch — keep awake permanently (ignores watch list)
- **Process watch list** — case-insensitive, `.exe` optional
- **Matching modes** — `--match exact|startswith|substr|regex` (default: `substr`)
- **Mouse jiggler (Windows)** — configurable **interval** and **pixel amplitude**; optional `--idle-threshold` to jiggle only after N seconds idle
- **Display vs System control** — `--display-only` or `--system-only` (or both defaults if unspecified)
- **Cross-platform** backends:
  - Windows: `SetThreadExecutionState` (+ optional jiggler)
  - macOS: `caffeinate`
  - Linux: `systemd-inhibit` → fallback to `gnome-session-inhibit` → `xdg-screensaver`
- **Clean shutdown** — signal handling (Ctrl+C / SIGTERM) for graceful exit
- **Verbose logging** with `--debug`
- **Zero Python dependencies** (stdlib only)

---

## Quick start
```bash
# 1) Clone
git clone https://github.com/<your-user>/auto-keep-awake-prevent-sleep-python.git
cd auto-keep-awake-prevent-sleep-python

# 2) Run (adjust filename if you renamed it)
python anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2
```

---

## Usage / Examples

```bash
# Always on + jiggle every 120s, 2px amplitude
python anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2

# Only keep awake when these apps run (FreeFileSync/RealTimeSync/OBS)
python anti-afk.py --watch "filesync,realtimesync,obs64" --jiggle --debug

# Fixed duration (2 hours), no jiggler
python anti-afk.py --duration 7200 --no-jiggle

# Faster process scanning (every 2s)
python anti-afk.py --watch "filesync" --poll 2

# Match mode example (prefix-only) + display-only
python anti-afk.py --watch "filesync" --match startswith --display-only

# Windows: jiggle only if idle for 5 minutes
python anti-afk.py --watch "obs64" --jiggle --idle-threshold 300
```

> **Tip:** Because of substring matching, `filesync` will match both **FreeFileSync.exe** and **RealTimeSync.exe**.

---

## CLI reference

```
--always-on / --no-always-on   : Force always-on mode on/off
--jiggle / --no-jiggle         : Enable/disable Windows mouse jiggler
--jiggle-interval <sec>        : Seconds between mini-moves (e.g., 50, 120)
--jiggle-pixels <px>           : Pixel amplitude per mini-move (±n px)
--idle-threshold <sec>         : Only jiggle if user idle time ≥ N seconds (Windows)
--watch "a,b,c"                : Comma-separated process names (case-insensitive)
--match exact|startswith|substr|regex
                               : Process match mode (default: substr)
--display-only                 : Prevent display sleep only
--system-only                  : Prevent system sleep only
--duration <sec>               : Keep awake for a fixed time
--poll <sec>                   : Process scan interval
--debug                        : Verbose logs
```

---

## Configuration (top of file)

Adjust the switches at the top of the script to set sensible defaults:

```python
ALWAYS_ON = True
MOUSE_JIGGLE_ENABLED = True
MOUSE_JIGGLE_INTERVAL_SEC = 120
MOUSE_JIGGLE_PIXELS = 2
POLL_SEC = 5
DEFAULT_WATCH = ["filesync","realtimesync","obs","obs64","audacity","reaper", ...]
```

### How process matching works

* Names are normalized to lowercase and `.exe` is ignored.
* Substring match: `"filesync"` matches `freefilesync`, `realtimesync`, etc.
* Works across platforms (`tasklist` on Windows, `ps` on Unix).

---

## Requirements

* **Python 3.9+**
* **Windows**: no admin rights required (uses `SetThreadExecutionState`; optional jiggler uses `SendInput`)
* **macOS**: uses `caffeinate`
* **Linux**: prefers `systemd-inhibit`; falls back to `gnome-session-inhibit` / `xdg-screensaver`

No external Python packages needed.

Recommended `.gitignore`:

```
__pycache__/
*.pyc
.venv/
.env
.vscode/
.DS_Store
```

---

## Troubleshooting

* **FreeFileSync not detected** → run with `--watch "filesync,realtimesync"` and add `--debug` to see matches.
* **Mouse cursor movement is distracting** → use `--no-jiggle` or set `MOUSE_JIGGLE_ENABLED = False` (Windows still stays awake via API).
* **macOS/Linux “nothing happens”** → ensure `caffeinate` / `systemd-inhibit` exists on your system; check PATH.
* **Corporate AV blocks jiggler** → disable jiggler; the OS inhibitor alone prevents sleep on Windows.

---

## Autostart (optional)

* **Windows**: Task Scheduler → *At log on* → Action:
  `python <path>\anti-afk.py --always-on --jiggle`
* **macOS**: add to Login Items or create a `launchd` agent.
* **Linux**: create a user `systemd` service or use `crontab @reboot`.

---

## Contributing

Issues and PRs welcome! Ideas: GUI tray toggle, per-app profiles, JSON config file.

---

## License

MIT — see `LICENSE`.

---

## Keywords / GitHub Topics

`python` `keep-awake` `prevent-sleep` `nosleep` `anti-idle` `mouse-jiggler` `windows` `macos` `linux` `cli` `freefilesync` `realtimesync` `obs`
