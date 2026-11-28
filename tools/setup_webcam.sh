#!/bin/sh
# One-time setup for the Gear 360 virtual webcams (run with sudo):
#   /dev/video10 "Gear 360"           normal view (follows the viewer), 1920x1080
#   /dev/video11 "Gear 360 equirect"  full 360° equirectangular picture, 2560x1280
#   /dev/video12 "OBS Virtual Camera" left free for OBS's own Virtual Camera button
# Installs v4l2loopback (DKMS), loads it at every boot with these devices, and loads it now.
set -e
if [ "$(id -u)" != 0 ]; then
    echo "run as root: sudo sh $0" >&2
    exit 1
fi
apt-get install -y v4l2loopback-dkms v4l2loopback-utils
cat > /etc/modprobe.d/g360-v4l2loopback.conf <<'EOF'
# Gear 360 virtual webcams (g360), plus one device for OBS's Virtual Camera
options v4l2loopback devices=3 video_nr=10,11,12 card_label="Gear 360,Gear 360 equirect,OBS Virtual Camera" exclusive_caps=1,1,1
EOF
echo v4l2loopback > /etc/modules-load.d/g360-v4l2loopback.conf
if lsmod | grep -q '^v4l2loopback'; then
    modprobe -r v4l2loopback || { echo "v4l2loopback is in use (close OBS and anything using a camera), then run this again" >&2; exit 1; }
fi
modprobe v4l2loopback
for d in /sys/class/video4linux/video1[012]; do
    echo "/dev/$(basename "$d"): $(cat "$d/name")"
done
