#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0 || { echo "Run with sudo on Ubuntu 24.04"; exit 1; }
apt-get update
apt-get install -y --no-install-recommends \
  git ca-certificates build-essential gcc-13 g++-13 cmake ninja-build pkg-config \
  autoconf automake libtool bison flex swig libsctp-dev libconfig-dev libfftw3-dev \
  libblas-dev liblapacke-dev libyaml-cpp-dev libsqlite3-dev libpcre2-dev zlib1g-dev \
  libgmp-dev libusb-1.0-0-dev libboost-all-dev libsimde-dev libcap-dev libuhd-dev \
  python3 python3-yaml python3-venv iproute2 iputils-ping iptables tcpdump curl \
  jq sqlite3 logrotate docker.io docker-compose-v2
echo 'Dependencies installed. Grant the deployment user Docker access separately.'
