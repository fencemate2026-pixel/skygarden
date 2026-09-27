# Pi install (Raspberry Pi 5, Raspberry Pi OS Bookworm)

```bash
sudo apt install -y python3-lgpio git
sudo useradd --system --home /var/lib/magickeys-detect --shell /usr/sbin/nologin mkdetect
sudo usermod -aG gpio mkdetect
sudo git clone <repo-url> /opt/magickeys-detect
sudo install -d -o mkdetect -g mkdetect -m 750 /var/lib/magickeys-detect
sudo install -d -m 750 /etc/magickeys-detect
sudo cp /opt/magickeys-detect/config/site.skygarden-foyer.example.json /etc/magickeys-detect/site.json
sudo install -m 600 -o root -g root /opt/magickeys-detect/.env.example /etc/magickeys-detect/agent.env
sudo nano /etc/magickeys-detect/agent.env     # ingest URL + this device's secret
sudo cp /opt/magickeys-detect/pi-agent/systemd/magickeys-detect.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now magickeys-detect
journalctl -u magickeys-detect -f
```

GPIO chip: on current Pi 5 kernels the header is `gpiochip0`; some older Pi 5 kernels exposed it as `gpiochip4`.
Check with `gpioinfo` and set `gpio.chip` in site.json. UNVERIFIED on the target Pi until checked.

Clock: signatures are rejected if the Pi clock is more than 5 minutes out. Keep NTP on; fit a DS3231 RTC as on Mildura.

This is a new Pi, not a live gate Pi. Network, Tailscale and SSH changes on it are still to be planned with a
self-restoring fallback once it is deployed.
