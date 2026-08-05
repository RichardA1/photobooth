#!/usr/bin/env python3
"""
Photobooth gallery + download server.

Runs on the Pi and is reached over the WiFi AP.
Serves a thumbnail grid; users can download individual JPEGs
or a ZIP of everything.
"""

import io
import os
import zipfile
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, send_from_directory, send_file, abort

PHOTO_DIR = Path(os.environ.get("PHOTOBOOTH_DIR", "/home/pi/photobooth/photos"))
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def _list_photos():
    files = sorted(
        (p for p in PHOTO_DIR.iterdir()
         if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": p.name,
            "size_kb": p.stat().st_size // 1024,
            "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d, %H:%M"),
        }
        for p in files
    ]


@app.route("/")
def index():
    return render_template("index.html", photos=_list_photos())


@app.route("/photo/<path:name>")
def photo(name):
    # send_from_directory guards against traversal
    return send_from_directory(PHOTO_DIR, name, as_attachment=False)


@app.route("/download/<path:name>")
def download(name):
    return send_from_directory(PHOTO_DIR, name, as_attachment=True)


@app.route("/download-all")
def download_all():
    photos = _list_photos()
    if not photos:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in photos:
            z.write(PHOTO_DIR / p["name"], arcname=p["name"])
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"photobooth_{stamp}.zip",
    )


if __name__ == "__main__":
    # Bind on all interfaces so it's reachable via the AP
    app.run(host="0.0.0.0", port=80)
