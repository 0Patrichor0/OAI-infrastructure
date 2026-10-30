#!/usr/bin/env bash
# 由 intentctl 生成：为第 $1 个 UE 准备独立 netns + veth（幂等，可反复执行）
set -u
IDX="$1"
NS="ue${IDX}"
HOST_IP="10.250.${IDX}.1/24"
NS_IP="10.250.${IDX}.2/24"

ip netns list | grep -qw "$NS" || ip netns add "$NS"
if ! ip link show "veth-$NS" >/dev/null 2>&1; then
  ip link add "veth-$NS" type veth peer name "veth-$NS-ns"
  ip link set "veth-$NS-ns" netns "$NS"
fi
ip addr show dev "veth-$NS" | grep -q "$HOST_IP" || ip addr add "$HOST_IP" dev "veth-$NS"
ip link set "veth-$NS" up
ip netns exec "$NS" ip addr show dev "veth-$NS-ns" | grep -q "$NS_IP" ||   ip netns exec "$NS" ip addr add "$NS_IP" dev "veth-$NS-ns"
ip netns exec "$NS" ip link set "veth-$NS-ns" up
ip netns exec "$NS" ip link set lo up
