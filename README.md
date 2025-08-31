# anti-afk-python-script

Cross-platform anti-AFK utility (Windows/macOS/Linux). Keeps the system awake via OS inhibitors; optional Windows mouse jiggle with configurable interval and pixel amplitude. Can auto-enable when certain processes are running (e.g. FreeFileSync/RealTimeSync, OBS).

## Features
- ALWAYS_ON switch (keep awake permanently)
- Process watch list (case-insensitive, `.exe` optional; substring match)
- Mouse jiggle (Windows): configurable `interval` and `pixels`
- Works with `caffeinate` (macOS) and `systemd-inhibit`/`gnome-session-inhibit` (Linux)

## Usage
```bash
# Always on + jiggle every 120s, 2px
python anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2

# Only when certain apps run (FreeFileSync/RealTimeSync/OBS)
python anti-afk.py --watch "filesync,realtimesync,obs64" --jiggle --debug

# Fixed duration (2 hours), no jiggle
python anti-afk.py --duration 7200 --no-jiggle
