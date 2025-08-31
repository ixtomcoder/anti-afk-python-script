# auto-keep-awake-prevent-sleep-python

> Keep your PC awake on **Windows / macOS / Linux**. Prevent sleep/idle using OS inhibitors, with optional **Windows mouse jiggler** (configurable interval & pixel amplitude). Can auto-enable while certain apps (e.g. **FreeFileSync / RealTimeSync / OBS**) are running.

![OS](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-informational)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-success)

---

## Features
- **ALWAYS_ON**: hält dauerhaft wach (ohne Prozess-Überwachung)
- **Process watch**: case-insensitive, `.exe` egal, **Substring-Match**
- **Mouse jiggler (Windows)**: Intervall **und** Pixel-Amplitude einstellbar
- **Cross-platform**: `caffeinate` (macOS), `systemd-inhibit` / `gnome-session-inhibit` (Linux)
- **Verbose logging** mit `--debug`

---

## Quick start
```bash
# 1) Klonen
git clone https://github.com/<your-user>/auto-keep-awake-prevent-sleep-python.git
cd auto-keep-awake-prevent-sleep-python

# 2) Starten (Dateiname ggf. anpassen, z.B. anti-afk.py)
python anti-afk.py --always-on --jiggle --jiggle-interval 120 --jiggle-pixels 2
