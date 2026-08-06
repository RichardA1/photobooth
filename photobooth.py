#!/usr/bin/env python3
"""
Photobooth - unified capture + web UI.

- Live MJPEG preview served over HTTP
- "Take Photo" button in browser triggers countdown + capture
- Physical button on GPIO 17 does the same
- NeoPixel ring flash (idle rainbow, countdown pulse, capture blast, review breath)
- Optional GPIO 5 secondary flash trigger (opto/MOSFET/SSR)
- Gallery with per-photo download + zip-all
"""

import io
import os
import sys
import time
import math
import signal
import logging
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Condition

from flask import (
    Flask, render_template, send_from_directory, send_file, abort,
    Response, jsonify,
)
from gpiozero import Button, DigitalOutputDevice
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from rpi_ws281x import PixelStrip, Color

# ---------- Configuration -------------------------------------------------

PHOTO_DIR      = Path(os.environ.get("PHOTOBOOTH_DIR", "/home/pi/photobooth/photos"))
GPIO_BUTTON    = 17          # PiTFT tactile pad #1 (or any external button to GND)
GPIO_FLASH     = 5           # optional secondary flash trigger
COUNTDOWN_FROM = 3
FLASH_MS       = 120
CAPTURE_HOLD_S = 0.35
STILL_SIZE     = (2028, 1520)   # IMX219 binned mode, ~3 MP, fast on Pi 3
PREVIEW_SIZE   = (640, 480)
DEBOUNCE_S     = 0.3
WEB_PORT       = 80

# NeoPixel ring
NEOPIXEL_COUNT      = 16
NEOPIXEL_PIN        = 12     # PWM0 alt (GPIO 18 is taken by PiTFT backlight)
NEOPIXEL_FREQ_HZ    = 800_000
NEOPIXEL_DMA        = 10
NEOPIXEL_BRIGHTNESS = 255
NEOPIXEL_INVERT     = False
NEOPIXEL_CHANNEL    = 0

# ---------- Logging -------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("photobooth")


# ---------- MJPEG streaming buffer ---------------------------------------

class StreamingOutput(io.BufferedIOBase):
    """picamera2 FileOutput target that holds the most recent JPEG frame."""

    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


# ---------- NeoPixel flash / mood ring -----------------------------------

class FlashRing:
    def __init__(self):
        self.strip = PixelStrip(
            NEOPIXEL_COUNT, NEOPIXEL_PIN, NEOPIXEL_FREQ_HZ,
            NEOPIXEL_DMA, NEOPIXEL_INVERT, NEOPIXEL_BRIGHTNESS,
            NEOPIXEL_CHANNEL,
        )
        self.strip.begin()
        self.mode = "idle"
        self.countdown_step = COUNTDOWN_FROM
        self._running = True
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def set_mode(self, mode, countdown_step=None):
        if countdown_step is not None:
            self.countdown_step = countdown_step
        self.mode = mode

    def stop(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self._fill(0, 0, 0)
        self.strip.show()

    def _fill(self, r, g, b):
        c = Color(int(r), int(g), int(b))
        for i in range(NEOPIXEL_COUNT):
            self.strip.setPixelColor(i, c)

    @staticmethod
    def _wheel(pos):
        pos = pos % 256
        if pos < 85:
            return (pos * 3, 255 - pos * 3, 0)
        if pos < 170:
            pos -= 85
            return (255 - pos * 3, 0, pos * 3)
        pos -= 170
        return (0, pos * 3, 255 - pos * 3)

    def _anim_idle(self, t):
        scale = 0.30
        offset = int(t * 40) & 0xFF
        for i in range(NEOPIXEL_COUNT):
            r, g, b = self._wheel(int(i * 256 / NEOPIXEL_COUNT) + offset)
            self.strip.setPixelColor(i, Color(int(r * scale),
                                              int(g * scale),
                                              int(b * scale)))

    def _anim_countdown(self, t):
        max_step = COUNTDOWN_FROM
        step = max(1, self.countdown_step)
        progress = 1.0 - (step - 1) / max_step
        pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 2)
        base = 0.35 + 0.55 * progress
        level = base * (0.7 + 0.3 * pulse)
        r = 255
        g = int(140 + (255 - 140) * progress)
        b = int(40  + (255 - 40)  * progress)
        self._fill(r * level, g * level, b * level)

    def _anim_capture(self, _t):
        self._fill(255, 255, 255)

    def _anim_review(self, t):
        level = 0.15 + 0.15 * (0.5 + 0.5 * math.sin(t * 2 * math.pi * 0.5))
        v = int(255 * level)
        self._fill(v, v, v)

    def _anim_off(self, _t):
        self._fill(0, 0, 0)

    def _loop(self):
        anims = {
            "idle":      self._anim_idle,
            "countdown": self._anim_countdown,
            "capture":   self._anim_capture,
            "review":    self._anim_review,
            "off":       self._anim_off,
        }
        while self._running:
            t = time.monotonic() - self._t0
            anims.get(self.mode, self._anim_idle)(t)
            self.strip.show()
            time.sleep(1 / 60)


# ---------- Main app -----------------------------------------------------

class Photobooth:
    def __init__(self):
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)

        # --- Camera ---------------------------------------------------
        self.cam = Picamera2()
        config = self.cam.create_video_configuration(
            main={"size": STILL_SIZE, "format": "RGB888"},
            lores={"size": PREVIEW_SIZE, "format": "YUV420"},
            buffer_count=4,
        )
        self.cam.configure(config)

        # Start hardware MJPEG encoder on the lores stream. Frames flow
        # into self.stream_output continuously; Flask serves the latest.
        self.stream_output = StreamingOutput()
        self.cam.start_recording(
            MJPEGEncoder(),
            FileOutput(self.stream_output),
            name="lores",
        )
        time.sleep(0.5)  # AE/AWB warm-up

        # --- GPIO -----------------------------------------------------
        self.button = Button(GPIO_BUTTON, pull_up=True, bounce_time=DEBOUNCE_S)
        self.flash_trigger = DigitalOutputDevice(GPIO_FLASH, active_high=True,
                                                 initial_value=False)
        self.button.when_pressed = self._on_button

        # --- Ring -----------------------------------------------------
        self.ring = FlashRing()
        self.ring.set_mode("idle")

        # --- State ----------------------------------------------------
        self.state_lock = threading.Lock()
        self.capturing = False
        self.state = "idle"          # idle | countdown | capture | review
        self.countdown_value = None  # "3" | "2" | "1" | "SMILE!" | None
        self.last_photo_name = None
        self.review_until = 0.0

        # --- Flask ----------------------------------------------------
        self.app = Flask(__name__)
        self._setup_routes()

    # ------------------------------------------------------------------

    def _setup_routes(self):
        app = self.app

        @app.route("/")
        def index():
            return render_template("index.html", photos=self._list_photos())

        @app.route("/stream.mjpg")
        def stream():
            def generate():
                while True:
                    with self.stream_output.condition:
                        self.stream_output.condition.wait()
                        frame = self.stream_output.frame
                    if not frame:
                        continue
                    yield (b"--FRAME\r\n"
                           b"Content-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(frame)).encode() +
                           b"\r\n\r\n" + frame + b"\r\n")
            return Response(generate(),
                            mimetype="multipart/x-mixed-replace; boundary=FRAME")

        @app.route("/status")
        def status():
            with self.state_lock:
                if self.state == "review" and time.monotonic() >= self.review_until:
                    self.state = "idle"
                return jsonify({
                    "state": self.state,
                    "countdown": self.countdown_value,
                    "latest_photo": self.last_photo_name,
                })

        @app.route("/capture", methods=["POST"])
        def capture():
            triggered = self._on_button()
            return jsonify({"triggered": triggered})

        @app.route("/photo/<path:name>")
        def photo(name):
            return send_from_directory(PHOTO_DIR, name)

        @app.route("/download/<path:name>")
        def download(name):
            return send_from_directory(PHOTO_DIR, name, as_attachment=True)

        @app.route("/download-all")
        def download_all():
            photos = self._list_photos()
            if not photos:
                abort(404)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in photos:
                    z.write(PHOTO_DIR / p["name"], arcname=p["name"])
            buf.seek(0)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return send_file(buf, mimetype="application/zip",
                             as_attachment=True,
                             download_name=f"photobooth_{stamp}.zip")

        @app.route("/gallery.json")
        def gallery_json():
            return jsonify(self._list_photos())

    # ------------------------------------------------------------------

    def _list_photos(self):
        files = sorted(
            (p for p in PHOTO_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [
            {"name": p.name,
             "size_kb": p.stat().st_size // 1024,
             "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d, %H:%M")}
            for p in files
        ]

    # ------------------------------------------------------------------

    def _on_button(self):
        with self.state_lock:
            if self.capturing:
                return False
            self.capturing = True
        threading.Thread(target=self._capture_sequence, daemon=True).start()
        return True

    def _capture_sequence(self):
        try:
            with self.state_lock:
                self.state = "countdown"

            for n in range(COUNTDOWN_FROM, 0, -1):
                with self.state_lock:
                    self.countdown_value = str(n)
                self.ring.set_mode("countdown", countdown_step=n)
                time.sleep(1.0)

            with self.state_lock:
                self.countdown_value = "SMILE!"
            time.sleep(0.3)

            self._fire_flash_async()
            self.ring.set_mode("capture")
            with self.state_lock:
                self.state = "capture"
                self.countdown_value = None

            path = self._capture_still()
            time.sleep(CAPTURE_HOLD_S)
            self.ring.set_mode("review")

            with self.state_lock:
                self.state = "review"
                self.last_photo_name = path.name
                self.review_until = time.monotonic() + 2.5

            time.sleep(2.5)
        except Exception:
            log.exception("Capture sequence failed")
        finally:
            with self.state_lock:
                self.state = "idle"
                self.countdown_value = None
                self.capturing = False
            self.ring.set_mode("idle")

    def _fire_flash_async(self):
        def pulse():
            self.flash_trigger.on()
            time.sleep(FLASH_MS / 1000.0)
            self.flash_trigger.off()
        threading.Thread(target=pulse, daemon=True).start()

    def _capture_still(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = PHOTO_DIR / f"photo_{ts}.jpg"
        request = self.cam.capture_request()
        request.save("main", str(path))
        request.release()
        log.info("Saved %s", path)
        return path

    # ------------------------------------------------------------------

    def run(self):
        signal.signal(signal.SIGTERM, lambda *a: self.shutdown())
        signal.signal(signal.SIGINT, lambda *a: self.shutdown())
        log.info("Photobooth ready. Button GPIO %d, web on port %d.",
                 GPIO_BUTTON, WEB_PORT)
        self.app.run(host="0.0.0.0", port=WEB_PORT,
                     threaded=True, use_reloader=False)

    def shutdown(self):
        log.info("Shutting down...")
        try:
            self.cam.stop_recording()
        except Exception:
            pass
        self.flash_trigger.off()
        try:
            self.ring.stop()
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    try:
        Photobooth().run()
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)
