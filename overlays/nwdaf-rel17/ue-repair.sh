#!/usr/bin/env bash
# UE / 用户面自愈阶梯（被 oran-ue-watchdog.timer 调用，也被 start-all.sh 收尾调用）
#
# 判据：oaitun_ue1 有 12.1.1.x  且  ping 192.168.73.135（ext-dn）通
#
# 阶梯：
#   L1  restart oran-ue                           ← UE 掉会话 / 隧道拿到 127.0.0.5
#   L2  recreate vpp-upf + restart oai-smf + SBI
#       + restart oran-ue                         ← UPF 的 PFCP 悄悄死了（NF 心跳还正常）
#   L3  start-all.sh --force（仅当 ALLOW_L3=1）    ← 前面都救不回来才全量重建
#
# 为什么要分两级：L2 会重建 UPF，代价是断掉所有会话，只有确认不是 UE 单侧问题时才做。
set -uo pipefail

ROOT=/home/ubuntu/nwdaf-rel17
CN5G_DIR=$ROOT/oai-cn5g-fed/docker-compose
CN5G=$CN5G_DIR/docker-compose-basic-vpp-nrf.yaml
LOG=/home/ubuntu/oai-ran-logs/ue-repair.log
LEVEL_FILE=$ROOT/.run/ue-repair.level
LOCK=$ROOT/.run/ue-repair.lock.d
ALLOW_L3="${UE_REPAIR_ALLOW_L3:-0}"

mkdir -p "$(dirname "$LOG")" "$ROOT/.run"
log() { local m; m="$(date '+%F %T') $*"; echo "$m"; echo "$m" >> "$LOG" 2>/dev/null || echo "$m" >> /var/tmp/ue-repair.log; }

# 同一时刻只允许一个实例（timer 3 分钟一次，L2 要跑 90 秒）
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') 已有实例在跑，跳过" >> "$LOG"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# start-all.sh 正在跑时不要插手（它会自己管 RAN 服务）
if [ "${UE_REPAIR_INSIDE:-0}" != 1 ] && [ -d "$ROOT/.run/start-all.lock.d" ]; then
  echo "$(date '+%F %T') start-all.sh 正在运行，本轮跳过" >> "$LOG"
  exit 0
fi

ue_ok() {
  ip -4 addr show oaitun_ue1 2>/dev/null | grep -q "inet 12.1.1" \
    && ping -c 2 -W 2 -I oaitun_ue1 192.168.73.135 >/dev/null 2>&1
}

# SMF 的 N4 是否死了：60 秒内没有 PFCP 心跳应答（msg type 2）= UPF 侧不通
n4_broken() {
  local l
  l=$(docker logs --since 60s oai-smf 2>&1) || return 0
  echo "$l" | grep -q "No UPF available\|PEER_NOT_RESPONDING" && return 0
  echo "$l" | grep -q "handle_receive_pfcp_msg msg type 2" || return 0
  return 1
}

restart_ue() {
  sudo systemctl restart oran-ue >/dev/null 2>&1 || systemctl restart oran-ue >/dev/null 2>&1
  sleep 35
}

if ue_ok; then
  rm -f "$LEVEL_FILE"
  exit 0
fi

log "检测到 UE/用户面异常（oaitun_ue1 缺失或 ping 不通）"

lvl=0
[ -f "$LEVEL_FILE" ] && lvl=$(cat "$LEVEL_FILE" 2>/dev/null || echo 0)

# N4 明显坏了就直接跳 L2，别在 L1 上浪费一个周期
if n4_broken && [ "$lvl" -lt 1 ]; then
  log "SMF 日志显示 UPF/PFCP 异常 → 跳过 L1，直接 L2"
  lvl=1
fi

if [ "$lvl" -lt 1 ]; then
  log "L1: 重启 oran-ue"
  restart_ue
  if ue_ok; then rm -f "$LEVEL_FILE"; log "L1 修复成功"; exit 0; fi
  log "L1 失败"
  echo 1 > "$LEVEL_FILE"
  lvl=1
fi

if [ "$lvl" -lt 2 ]; then
  log "L2: 重建 vpp-upf → 重启 SMF → 重启 SBI → 重启 UE"
  python3 /home/ubuntu/intent-driven/tools/apply_state.py >/dev/null 2>&1 || true
  ( cd "$CN5G_DIR" && docker compose -f "$(basename "$CN5G")" up -d --force-recreate --no-deps vpp-upf ) >>"$LOG" 2>&1
  sleep 25
  docker restart oai-smf >>"$LOG" 2>&1
  sleep 25
  docker restart oai-nwdaf-sbi >>"$LOG" 2>&1
  restart_ue
  if ue_ok; then rm -f "$LEVEL_FILE"; log "L2 修复成功（UE $(ip -4 addr show oaitun_ue1 | awk '/inet/{print $2}')）"; exit 0; fi
  log "L2 失败"
  echo 2 > "$LEVEL_FILE"
  lvl=2
fi

if [ "$ALLOW_L3" = 1 ]; then
  log "L3: 全量重建（start-all.sh --force），本次会跑 6~8 分钟"
  if bash "$ROOT/start-all.sh" --force >>"$LOG" 2>&1; then
    rm -f "$LEVEL_FILE"
    log "L3 完成"
    exit 0
  fi
  log "L3 仍失败，需要人工介入"
  exit 1
fi

log "已升到 L2 仍未恢复；如需全量重建请手动跑: $ROOT/start-all.sh --force"
exit 1
