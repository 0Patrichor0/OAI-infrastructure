#!/usr/bin/env bash
set -euo pipefail
LAB_HOME="${1:?Usage: bash scripts/build_asn1c.sh /home/ubuntu}"
SRC="$LAB_HOME/build-tools/asn1c"
test -f "$SRC/configure.ac" || { echo "Run fetch_sources.py first"; exit 1; }
cd "$SRC"
autoreconf -iv
CFLAGS='-O2 -fno-strict-aliasing' ./configure --prefix=/opt/asn1c
make -j3
sudo make install
