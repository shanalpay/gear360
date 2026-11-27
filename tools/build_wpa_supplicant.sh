#!/bin/sh
# Build a private wpa_supplicant 2.10 for `native_session.py --p2p own` (no root needed to build).
#
# Why: the Gear 360 only talks Wi-Fi Direct to the P2P device address it derives from the announced
# `wifi-mac-address` (that MAC with the locally-administered bit set). Stock wpa_supplicant always creates a
# dedicated P2P-device wdev when the driver offers one, and mac80211 gives that wdev a hardware address that
# cannot be chosen. This build drops that one capability flag, so P2P runs on the Wi-Fi netdev itself and the
# P2P device address is the interface MAC, which g360.p2p sets for the session.
#
# Output: tmp/wpa-build/wpa_supplicant (libnl linked statically from the distro -dev packages, fetched with
# `apt download` into tmp/, so nothing is installed system-wide).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
B="$ROOT/tmp/wpa-build"
VER=2.10
SHA=20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f
mkdir -p "$B/debs" "$B/sysroot" "$B/src"
cd "$B"

if [ ! -d sysroot/usr/include/libnl3 ]; then
    (cd debs && apt download libnl-3-dev libnl-genl-3-dev)
    for d in debs/*.deb; do dpkg -x "$d" sysroot; done
fi
[ -f "wpa_supplicant-$VER.tar.gz" ] || curl -sfLO "https://w1.fi/releases/wpa_supplicant-$VER.tar.gz"
echo "$SHA  wpa_supplicant-$VER.tar.gz" | sha256sum -c -
rm -rf "src/wpa_supplicant-$VER"
tar -xzf "wpa_supplicant-$VER.tar.gz" -C src
W="$B/src/wpa_supplicant-$VER"

python3 - "$W/src/drivers/driver_nl80211_capa.c" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
old = """		case NL80211_IFTYPE_P2P_DEVICE:
			info->capa->flags |=
				WPA_DRIVER_FLAGS_DEDICATED_P2P_DEVICE;
			break;"""
new = """		case NL80211_IFTYPE_P2P_DEVICE:
			/* g360: run P2P on the netdev itself, so the P2P
			 * device address is the interface's (settable) MAC */
			break;"""
assert s.count(old) == 1, "patch target not found"
open(p, "w").write(s.replace(old, new))
EOF

cat > "$W/wpa_supplicant/.config" <<'EOF'
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL32=y
CONFIG_CTRL_IFACE=y
CONFIG_P2P=y
CONFIG_AP=y
CONFIG_WPS=y
CONFIG_IEEE80211N=y
CONFIG_IEEE80211AC=y
CONFIG_TLS=openssl
CONFIG_DEBUG_FILE=y
CONFIG_BACKEND=file
CONFIG_ELOOP=eloop
CONFIG_L2_PACKET=linux
EOF

L="$B/sysroot/lib/x86_64-linux-gnu"
cd "$W/wpa_supplicant"
PKG_CONFIG_PATH="$B/sysroot/usr/lib/x86_64-linux-gnu/pkgconfig" PKG_CONFIG_SYSROOT_DIR="$B/sysroot" \
    make -j"$(nproc)" EXTRA_CFLAGS="-I$B/sysroot/usr/include/libnl3" \
    LDFLAGS="-L$L -L$B/sysroot/usr/lib/x86_64-linux-gnu" wpa_supplicant >"$B/build.log" 2>&1 \
    || { tail -20 "$B/build.log"; exit 1; }
cp wpa_supplicant "$B/wpa_supplicant"
"$B/wpa_supplicant" -v | head -1
echo "built $B/wpa_supplicant"
