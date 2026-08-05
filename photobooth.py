#!/usr/bin/env python3
"""
Photobooth - live preview + countdown + capture on a Pi with TFT display.

Hardware:
  - Raspberry Pi 3 (or newer)
  - Pi Camera (any generation supported by libcamera / picamera2)
  - TFT display configured as primary framebuffer
  - Trigger button between GPIO_BUTTON and GND
  - NeoPixel ring on GPIO 18 (PWM) - primary "flash" / modeling light
  - Optional secondary flash trigger on GPIO_FLASH (drive an opto/MOSFET/relay)
"""

import os
import sys
import time
import math
import signal
import logging
import threading
from datetime import datetime
from pathlib import Path

import pygame
from gpiozero import Button, DigitalOutputDevice
from picamera2 import Picamera2
from rpi_ws281x import PixelStrip, Color

# ---------- Configuration -------------------------------------------------

PHOTO_DIR      = Path(os.environ.get("PHOTOBOOTH_DIR", "/home/pi/photobooth/photos"))
GPIO_BUTTON    = 17          # matches PiTFT tactile switch pad #1
GPIO_FLASH     = 5           # secondary flash trigger (opto / MOSFET / SSR)
COUNTDOWN_FROM = 3           # seconds
FLASH_MS       = 120         # secondary flash pulse length in ms
CAPTURE_HOLD_S = 0.35        # how long the ring stays full-white during capture
CAPTURE_SIZE   = (3280, 2464)   # Pi Cam v2.1 (IMX219) native 8MP
PREVIEW_SIZE   = (640, 480)
FPS            = 30
FONT_NAME      = None
DEBOUNCE_S     = 0.3

# NeoPixel ring
NEOPIXEL_COUNT       = 16    # 12 / 16 / 24
NEOPIXEL_PIN         = 12    # PWM0 alternate (GPIO 18 is taken by PiTFT backlight)
NEOPIXEL_FREQ_HZ     = 800_000
NEOPIXEL_DMA         = 10
NEOPIXEL_BRIGHTNESS  = 255   # 0-255, overall cap; per-mode brightness is scaled below
NEOPIXEL_INVERT      = False
NEOPIXEL_CHANNEL     = 0

# ---------- Logging -------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("photobooth")


# ---------- NeoPixel flash / mood ring ------------------------------------

class FlashRing:
    """
    Background thread that animates a NeoPixel ring based on `mode`.

    Modes:
      idle       - slow rainbow chase, ~30% brightness
      countdown  - pulses that get brighter/whiter as the count drops
      capture    - solid full-white blast
      review     - gentle breathing white while the last photo is shown
      off        - all pixels dark
    """

    def __init__(self):
        self.strip = PixelStrip(
            NEOPIXEL_COUNT, NEOPIXEL_PIN, NEOPIXEL_FREQ_HZ,
            NEOPIXEL_DMA, NEOPIXEL_INVERT, NEOPIXEL_BRIGHTNESS,
            NEOPIXEL_CHANNEL,
        )
        self.strip.begin()
        self.mode = "idle"
        self.countdown_step = COUNTDOWN_FROM  # updated by capture sequence
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

    # --- helpers -----------------------------------------------------

    def _fill(self, r, g, b):
        c = Color(int(r), int(g), int(b))
        for i in range(NEOPIXEL_COUNT):
            self.strip.setPixelColor(i, c)

    @staticmethod
    def _wheel(pos):
        """0-255 -> RGB rainbow."""
        pos = pos % 256
        if pos < 85:
            return (pos * 3, 255 - pos * 3, 0)
        if pos < 170:
            pos -= 85
            return (255 - pos * 3, 0, pos * 3)
        pos -= 170
        return (0, pos * 3, 255 - pos * 3)

    # --- animations --------------------------------------------------

    def _anim_idle(self, t):
        # Rainbow chase, moderate brightness
        scale = 0.30
        offset = int(t * 40) & 0xFF
        for i in range(NEOPIXEL_COUNT):
            r, g, b = self._wheel(int(i * 256 / NEOPIXEL_COUNT) + offset)
            self.strip.setPixelColor(i, Color(int(r * scale),
                                              int(g * scale),
                                              int(b * scale)))

    def _anim_countdown(self, t):
        # As countdown_step goes 3 -> 2 -> 1, ramp brightness and shift
        # color from warm amber toward white. Pulse within each second.
        max_step = COUNTDOWN_FROM
        step = max(1, self.countdown_step)
        progress = 1.0 - (step - 1) / max_step   # 0..1 through the countdown
        # 2 Hz pulse
        pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 2)
        base = 0.35 + 0.55 * progress            # brightness floor rises
        level = base * (0.7 + 0.3 * pulse)
        # Color: amber (255, 140, 40) -> white (255, 255, 255)
        r = 255
        g = int(140 + (255 - 140) * progress)
        b = int(40  + (255 - 40)  * progress)
        self._fill(r * level, g * level, b * level)

    def _anim_capture(self, _t):
        self._fill(255, 255, 255)

    def _anim_review(self, t):
        # Gentle breathing white
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
            time.sleep(1 / 60)  # ~60 fps ring updates


# ---------- Main app ------------------------------------------------------

class Photobooth:
    def __init__(self):
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)

        # --- Display ---------------------------------------------------
        # SDL_VIDEODRIVER is provided via systemd (or the shell). Typical
        # values on the PiTFT:
        #   - fbcp mirror mode: leave unset (uses default) or "kmsdrm"
        #   - Adafruit "console" install: SDL_VIDEODRIVER=fbcon SDL_FBDEV=/dev/fb1
        pygame.init()
        pygame.mouse.set_visible(False)
        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        self.w, self.h = self.screen.get_size()
        log.info("Display: %dx%d", self.w, self.h)

        self.font_big   = pygame.font.Font(FONT_NAME, max(48, self.h // 3))
        self.font_small = pygame.font.Font(FONT_NAME, max(14, self.h // 20))

        # --- Camera ----------------------------------------------------
        self.cam = Picamera2()
        cfg = self.cam.create_still_configuration(
            main={"size": CAPTURE_SIZE, "format": "RGB888"},
            lores={"size": PREVIEW_SIZE, "format": "RGB888"},
            display=None,
            buffer_count=3,
        )
        self.cam.configure(cfg)
        self.cam.start()
        time.sleep(0.5)

        # --- GPIO ------------------------------------------------------
        self.button = Button(GPIO_BUTTON, pull_up=True, bounce_time=DEBOUNCE_S)
        self.flash_trigger = DigitalOutputDevice(GPIO_FLASH, active_high=True,
                                                 initial_value=False)
        self.button.when_pressed = self._on_button

        # --- NeoPixel ring --------------------------------------------
        self.ring = FlashRing()
        self.ring.set_mode("idle")

        # --- State -----------------------------------------------------
        self.state_lock = threading.Lock()
        self.capturing  = False
        self.last_photo = None
        self.last_photo_until = 0
        self._countdown_value = None
        self.running = True

        signal.signal(signal.SIGTERM, lambda *a: self.stop())
        signal.signal(signal.SIGINT,  lambda *a: self.stop())

    # ------------------------------------------------------------------

    def stop(self):
        log.info("Shutting down...")
        self.running = False

    def _on_button(self):
        with self.state_lock:
            if self.capturing:
                return
            self.capturing = True
        threading.Thread(target=self._capture_sequence, daemon=True).start()

    # ------------------------------------------------------------------

    def _capture_sequence(self):
        try:
            self._countdown()
            self._fire_flash_async()
            self.ring.set_mode("capture")
            path = self._capture_still()
            # Hold the white for a beat so the ring actually lights the shot
            time.sleep(CAPTURE_HOLD_S)
            self.ring.set_mode("review")
            self._show_result(path)
        except Exception:
            log.exception("Capture sequence failed")
        finally:
            # Wait out the review window before going back to idle animation
            while self.last_photo and time.monotonic() < self.last_photo_until:
                time.sleep(0.05)
            self.ring.set_mode("idle")
            with self.state_lock:
                self.capturing = False

    def _countdown(self):
        for n in range(COUNTDOWN_FROM, 0, -1):
            self._countdown_value = str(n)
            self.ring.set_mode("countdown", countdown_step=n)
            time.sleep(1.0)
        self._countdown_value = "SMILE!"
        time.sleep(0.3)
        self._countdown_value = None

    def _fire_flash_async(self):
        def pulse():
            self.flash_trigger.on()
            time.sleep(FLASH_MS / 1000.0)
            self.flash_trigger.off()
        threading.Thread(target=pulse, daemon=True).start()

    def _capture_still(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = PHOTO_DIR / f"photo_{ts}.jpg"
        self.cam.capture_file(str(path), name="main")
        log.info("Saved %s", path)
        return path

    def _show_result(self, path):
        try:
            img = pygame.image.load(str(path))
            img = pygame.transform.scale(img, (self.w, self.h))
            self.last_photo = img
            self.last_photo_until = time.monotonic() + 2.5
        except Exception:
            log.exception("Could not load %s for preview", path)

    # ------------------------------------------------------------------

    def _draw_preview_frame(self):
        frame = self.cam.capture_array("lores")
        surf = pygame.image.frombuffer(frame.tobytes(),
                                       (frame.shape[1], frame.shape[0]),
                                       "RGB")
        surf = pygame.transform.scale(surf, (self.w, self.h))
        self.screen.blit(surf, (0, 0))

    def _draw_countdown(self):
        val = self._countdown_value
        if not val:
            return
        text   = self.font_big.render(val, True, (255, 255, 255))
        shadow = self.font_big.render(val, True, (0, 0, 0))
        rect = text.get_rect(center=(self.w // 2, self.h // 2))
        self.screen.blit(shadow, rect.move(4, 4))
        self.screen.blit(text, rect)

    def _draw_idle_hint(self):
        if self.capturing:
            return
        msg = "Press the button to take a photo"
        text   = self.font_small.render(msg, True, (255, 255, 255))
        shadow = self.font_small.render(msg, True, (0, 0, 0))
        rect = text.get_rect(midbottom=(self.w // 2, self.h - 20))
        self.screen.blit(shadow, rect.move(2, 2))
        self.screen.blit(text, rect)

    # ------------------------------------------------------------------

    def run(self):
        clock = pygame.time.Clock()
        log.info("Photobooth ready. Waiting for button on GPIO %d.", GPIO_BUTTON)
        while self.running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.stop()
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    self.stop()
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_SPACE:
                    self._on_button()

            if self.last_photo and time.monotonic() < self.last_photo_until:
                self.screen.blit(self.last_photo, (0, 0))
            else:
                self.last_photo = None
                self._draw_preview_frame()
                self._draw_countdown()
                self._draw_idle_hint()

            pygame.display.flip()
            clock.tick(FPS)

        # Cleanup
        try:
            self.cam.stop()
        except Exception:
            pass
        self.flash_trigger.off()
        self.ring.stop()
        pygame.quit()


if __name__ == "__main__":
    try:
        Photobooth().run()
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)
