#!/usr/bin/env bash
# Photobooth setup for Raspberry Pi OS Bookworm.
# Run as: sudo bash setup.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run me with sudo." >&2; exit 1
fi

PI_USER=${SUDO_USER:-pi}
APP_DIR="/home/${PI_USER}/photobooth"
SSID="Photobooth"
PSK="photobooth123"      # change this
HOTSPOT_NAME="photobooth-ap"

echo "==> Installing system packages"
apt update
apt install -y \
  python3-picamera2 \
  python3-pygame \
  python3-gpiozero \
  python3-flask \
  python3-libgpiod \
  python3-pip \
  network-manager

echo "==> Installing rpi_ws281x for NeoPixels"
pip3 install --break-system-packages rpi_ws281x

echo "==> Disabling onboard audio (conflicts with PWM0 / GPIO 18 used for NeoPixels)"
CONFIG=/boot/firmware/config.txt
[[ -f $CONFIG ]] || CONFIG=/boot/config.txt   # older Pi OS layout
if grep -qE '^\s*dtparam=audio=on' "$CONFIG"; then
  sed -i 's/^\s*dtparam=audio=on/dtparam=audio=off/' "$CONFIG"
elif ! grep -qE '^\s*dtparam=audio=off' "$CONFIG"; then
  echo "dtparam=audio=off" >> "$CONFIG"
fi

echo "==> Copying app files to ${APP_DIR}"
mkdir -p "${APP_DIR}/photos"
cp -r photobooth.py gallery_server.py templates "${APP_DIR}/"
chown -R "${PI_USER}:${PI_USER}" "${APP_DIR}"

echo "==> Installing systemd services"
cp systemd/photobooth.service /etc/systemd/system/
cp systemd/photobooth-gallery.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable photobooth.service
systemctl enable photobooth-gallery.service

echo "==> Configuring WiFi access point via NetworkManager"
# Remove any existing hotspot with the same name
nmcli connection delete "${HOTSPOT_NAME}" >/dev/null 2>&1 || true

# Create an AP-mode connection. Pi will be 10.42.0.1 (NM default for shared mode).
nmcli connection add type wifi ifname wlan0 con-name "${HOTSPOT_NAME}" \
  autoconnect yes ssid "${SSID}"
nmcli connection modify "${HOTSPOT_NAME}" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "${PSK}"
nmcli connection up "${HOTSPOT_NAME}" || true

echo
echo "============================================================"
echo " Setup complete."
echo
echo "  SSID:     ${SSID}"
echo "  Password: ${PSK}"
echo "  Gallery:  http://10.42.0.1/"
echo
echo " Reboot with: sudo reboot"
echo "============================================================"
