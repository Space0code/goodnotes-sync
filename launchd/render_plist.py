"""Render the launchd plist template: daytime StartCalendarInterval slots and WatchPaths from settings.json.

Usage: render_plist.py TEMPLATE SETTINGS APP_DIR HOME > plist
"""
import json
import os
import sys
from xml.sax.saxutils import escape

template, settings_file, app, home = sys.argv[1:]
with open(settings_file) as f:
    s = json.load(f)
start, end = s.get("active_hours", ["08:00", "21:00"])
every = int(s.get("check_every_minutes", 15))
first, last = (int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for t in (start, end))
slots = "\n".join(f"    <dict><key>Hour</key><integer>{t // 60}</integer>"
                  f"<key>Minute</key><integer>{t % 60}</integer></dict>" for t in range(first, last + 1, every))
watch = os.path.expanduser(s.get("watch_path", "~/Library/Application Support/aula-sync/state.json"))
watch_xml = (f"  <key>WatchPaths</key>\n  <array>\n    <string>{escape(watch)}</string>\n  </array>"
             if watch and os.path.isdir(os.path.dirname(watch)) else "")
out = (open(template).read().replace("__APP__", escape(app)).replace("__HOME__", escape(home))
       .replace("__CALENDAR__", slots).replace("__WATCH__\n", watch_xml + "\n" if watch_xml else ""))
sys.stdout.write(out)
