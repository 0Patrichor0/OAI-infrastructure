#!/usr/bin/env bash
# 持续在 UE 的 TUN 上造上下行流量，保证 SMF 有 usage report、NWDAF 闭环有数据
set -u
DN=192.168.73.135          # oai-ext-dn
IF=oaitun_ue1
for i in $(seq 1 60); do ip -4 addr show "$IF" 2>/dev/null | grep -q "inet 12.1.1" && break; sleep 2; done
exec ping -i 0.05 -I "$IF" "$DN"
