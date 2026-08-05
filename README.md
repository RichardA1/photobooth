# Pi Photobooth

Live-preview photobooth for Raspberry Pi 3 + Pi Camera + TFT display, with:

- Live camera preview on the TFT
- Physical trigger button
- 3-2-1 countdown overlay
- GPIO output pulse to fire an external (non-neopixel) flash
- Post-capture photo review
- Standalone WiFi access point so guests can browse and download photos from their phones

## Hardware

| Function | Pin (BCM) | Notes |
|---|---|---|
| Trigger button | GPIO **17** → GND | The PiTFT has a tactile switch pad on GPIO 17 — populate that switch (comes with the kit) and it *is* your trigger. Or wire an external button in parallel. |
| NeoPixel data  | GPIO **12** (PWM0 alt) | On the Pi header, above the PiTFT. GPIO 18 is unavailable because the PiTFT uses it for backlight PWM. |
| Secondary flash trigger | GPIO **5** | Optional. Pulsed at capture in parallel with the ring — feed into an opto/MOSFET/SSR to fire a real strobe. GPIO 27 is unavailable (PiTFT tactile switch pad). |
| Camera | CSI ribbon | Pi Camera v2.1 (IMX219), silver contacts face the HDMI port. |
| TFT | 2×20 header | Adafruit PiTFT 2.8" (320×240). See setup below. |

Change the pin numbers at the top of `photobooth.py` if you want different ones.

### PiTFT 2.8" install (do this first, before running `setup.sh`)

The PiTFT is an SPI framebuffer, not HDMI, so the Pi needs Adafruit's driver installed before pygame can render to it. Their installer handles kernel overlay, `/dev/fb1`, touch driver, and console remapping in one shot.

```bash
# On a fresh Bookworm Lite install
sudo apt update
sudo apt install -y git python3-pip python3-venv
python3 -m venv --system-site-packages ~/pitft-env
source ~/pitft-env/bin/activate
pip install --upgrade adafruit-python-shell click Flask-SQLAlchemy
git clone https://github.com/adafruit/Raspberry-Pi-Installer-Scripts.git
cd Raspberry-Pi-Installer-Scripts

# 2.8" RESISTIVE (STMPE touch controller). Use --display=28c if capacitive.
sudo -E env PATH=$PATH python3 adafruit-pitft.py \
    --display=28r --rotation=90 --install-type=console
```

Answer "yes" to reboot when it prompts. After it comes back up you should see the console text on the TFT.

**Choosing an `--install-type`:**

- `console` — TFT is the primary console. `/dev/fb1` is the TFT. Set `SDL_VIDEODRIVER=fbcon` and `SDL_FBDEV=/dev/fb1` in the systemd unit (this is the default in `photobooth.service`). Best perf.
- `mirror` — Runs `fbcp` to copy HDMI to the TFT. Pygame renders to HDMI as normal. Slightly heavier CPU but works with any graphics program without special env vars. If you go this route, comment out the `SDL_VIDEODRIVER`/`SDL_FBDEV` lines in the service.

The **resistive vs capacitive** flag matters: `28r` uses the STMPE touch controller (and STMPE also controls the backlight); `28c` uses a different touch chip and reserves **GPIO 18 for backlight PWM**. Either way, our design already avoids GPIO 18, so the rest works with both variants.

### Then run our setup

```bash
cd /home/pi/photobooth
sudo bash setup.sh
sudo reboot
```

After reboot:

- The photobooth app comes up on the TFT
- The `Photobooth` WiFi SSID is broadcasting (default password `photobooth123` — change it in `setup.sh`)
- Connect a phone/laptop, open `http://10.42.0.1/` for the gallery

## Manual run (for dev)

```bash
# On the Pi
python3 photobooth.py            # SPACE bar also triggers a capture
python3 gallery_server.py        # in another shell, then browse http://localhost/
```

## Files

```
photobooth/
├── photobooth.py           # capture app (pygame + picamera2 + gpiozero)
├── gallery_server.py       # Flask gallery/download server
├── templates/index.html    # gallery UI
├── systemd/*.service       # unit files for both apps
├── setup.sh                # deps, WiFi AP, service enable
└── README.md
```

## Configuration knobs

Environment variable `PHOTOBOOTH_DIR` sets where photos are written (default `/home/pi/photobooth/photos`). Both the capture app and the gallery server read it, so they always agree.

Inside `photobooth.py`:

- `COUNTDOWN_FROM` — seconds to count down (default 3)
- `FLASH_MS` — flash pulse width in ms (default 120)
- `CAPTURE_SIZE` — still resolution; drop to `(2028, 1520)` for HQ cam binned mode
- `PREVIEW_SIZE` — lores stream used for the live view

## Troubleshooting

- **Blank TFT / preview on HDMI instead**: SDL is picking the wrong display. Try `SDL_VIDEODRIVER=fbcon` or `SDL_FBDEV=/dev/fb1` in the service file's `Environment=` lines.
- **`picamera2` import errors**: you're probably on a pre-Bookworm image without libcamera. Either upgrade or fall back to the old `picamera` module (would need code changes).
- **Hotspot doesn't come up**: check `nmcli connection show`. If wlan0 is claimed by wpa_supplicant, disable the old `dhcpcd`/`wpa_supplicant` stack — Bookworm should already be on NetworkManager.
- **Button fires twice**: increase `DEBOUNCE_S` in `photobooth.py`.

## NeoPixel flash wiring

The ring is animated continuously (rainbow chase while idle → warm pulsing during countdown → full-white blast at capture → soft breathing during photo review). Change the ring size at the top of `photobooth.py`:

```python
NEOPIXEL_COUNT = 16    # 12 / 16 / 24
```

Wiring:

- **Ring 5V** → external 5V supply (**not** the Pi's 5V rail for anything bigger than a 12-pixel ring)
- **Ring GND** → supply GND **and** Pi GND (common ground is mandatory)
- **Ring DIN** → GPIO 18 through a **330–470 Ω resistor** placed as close to the ring as possible
- **1000 µF electrolytic** across ring 5V/GND, near the ring

Power budget: WS2812 pixels draw up to ~60 mA at full white each. A 16-pixel ring at capture = ~1 A peak. A **5V / 3A** supply is a safe pick for any of 12/16/24.

If you see flicker or wrong colors, the Pi's 3.3 V data isn't quite enough headroom for the WS2812's 5 V logic. A **74AHCT125** level shifter between GPIO 18 and DIN fixes it reliably.

`dtparam=audio=off` is required because the onboard audio driver shares PWM0 with GPIO 18. `setup.sh` sets this for you.

## Optional real flash (GPIO 27)

GPIO 27 gets a short pulse at the same moment the ring hits full white. It's 3.3 V logic and only sources ~16 mA — treat it as a **signal**, not power:

- Optocoupler across a hot-shoe center pin / PC-sync connector, or
- MOSFET / SSR that switches the flash's own supply

Never wire a strobe's trigger contacts directly to the Pi.
