#!/usr/bin/env bash
# 一键拉起整套测试床（关机重启后跑这个）
#
# 每一步都是踩过坑才这么写的：
#   1) 先停 O-RAN 宿主服务（避免半死状态残留）
#   2) 5GC：默认"先体检再动手"
#        · 9 个容器健康 且 SMF 已和 UPF 建立 N4 关联 → 不折腾，跳过
#        · 否则轻量修：recreate vpp-upf + 重启 SMF（30 秒级）
#        · 还不行才全量 down/up（它俩挂在 5GC 网络上，AnLF/MTLF 也要重建）
#      vpp-upf 是真 VPP：docker restart 之后 PFCP 栈起不来，SMF 会永久报
#      "No UPF available"，PDU 会话被拒(cause 0x21) —— 必须 recreate，不能 restart。
#   3) SBI 重新订阅（内存态，SMF 一重启就丢，丢了 Mongo 收不到数据）
#   4) O-RAN：ric → cu-cp → cu-up → du → ue → traffic → 两个 xApp
#   5) 自检 + 自愈：SCTP、UE 的 IP 且真 ping 通、NWDAF 数据增长、KPM 上报、AI Fabric
#      自检不过就调 ue-repair.sh 走阶梯修复（L1 重启 UE / L2 重建 UPF+SMF）
#
# 用法:
#   ./start-all.sh            体检 + 按需修复（日常用这个）
#   ./start-all.sh --force    强制 5GC 冷重建（怀疑状态烂透了）
#   ./start-all.sh --quick    完全不动 5GC，只补 AnLF/MTLF + RAN
#   ./start-all.sh --check    只读检查脚本和修复配置，不启动/停止服务
set -uo pipefail

ROOT=/home/ubuntu/nwdaf-rel17
LOGS=/home/ubuntu/oai-ran-logs
CN5G_DIR=$ROOT/oai-cn5g-fed/docker-compose
CN5G=docker-compose-basic-vpp-nrf.yaml
MODE=smart
case "${1:-}" in
  --force) MODE=force ;;
  --quick) MODE=quick ;;
  --check) MODE=check ;;
esac

if [ "$MODE" = check ]; then
  bash -n "$0" || exit 1
  bash -n "$ROOT/systemd/oai-nf-watchdog.sh" || exit 1
  [[ "$(systemctl show flexric-ric -p Environment --value)" == *FLEXRIC_STATS_LOG=0* ]] || {
    echo "缺少 RIC 日志保护配置 FLEXRIC_STATS_LOG=0"; exit 1;
  }
  [[ "$(systemctl show oai-nf-watchdog -p RuntimeDirectory --value)" == oai-nf-watchdog ]] || {
    echo "缺少 watchdog RuntimeDirectory 配置"; exit 1;
  }
  echo "只读检查通过：脚本语法、RIC 日志保护、watchdog 运行目录正常"
  exit 0
fi

# docker 组权限：当前 shell 不在组里就把整个脚本用 sg 重新拉起来
if ! docker info >/dev/null 2>&1; then
  exec sg docker -c "bash '$0' $*"
fi
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# 全局锁：ue-repair.sh（看门狗）看到这把锁就知道 start-all 正在跑，不要去抢 RAN 服务
RUNDIR=$ROOT/.run; mkdir -p "$RUNDIR"
LOCKDIR=$RUNDIR/start-all.lock.d
# 清理僵死锁（超过 20 分钟）
[ -d "$LOCKDIR" ] && [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +20 2>/dev/null)" ] && rmdir "$LOCKDIR" 2>/dev/null
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "另一个 start-all.sh 正在运行，退出"
  exit 1
fi
PAUSED_TIMERS=()
trap cleanup_startup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠️  $*"; }
step() { echo; echo "== $* =="; }

# BEGIN STARTUP HELPERS
restore_automation() {
  local timer
  for timer in "${PAUSED_TIMERS[@]}"; do
    $SUDO systemctl start "$timer" >/dev/null 2>&1 || warn "恢复 $timer 失败，请检查"
  done
  PAUSED_TIMERS=()
}

cleanup_startup() {
  local rc=$?
  trap - EXIT
  rmdir "$LOCKDIR" 2>/dev/null || true
  restore_automation
  exit "$rc"
}

pause_automation() {
  local timer unit own_cgroup waiting round
  own_cgroup=$(cat /proc/$$/cgroup)
  for timer in oran-ue-watchdog.timer xapp-kpm-watchdog.timer oai-nf-watchdog.timer nwdaf-deployment-watchdog.timer flexric-guard.timer intent-state-apply.timer; do
    if systemctl is-active --quiet "$timer"; then
      PAUSED_TIMERS+=("$timer")
      $SUDO systemctl stop "$timer" || return 1
    fi
  done
  # Do not terminate an in-flight recovery. L3 may itself be our parent unit.
  for round in $(seq 1 120); do
    waiting=0
    for unit in oran-ue-watchdog.service xapp-kpm-watchdog.service oai-nf-watchdog.service nwdaf-deployment-watchdog.service flexric-guard.service intent-state-apply.service; do
      [[ "$own_cgroup" == *"/$unit"* ]] && continue
      case "$(systemctl show "$unit" -p ActiveState --value)" in
        active|activating|deactivating) waiting=1 ;;
      esac
    done
    [ "$waiting" = 0 ] && return 0
    sleep 1
  done
  warn "后台恢复任务仍未结束，退出以免并发重建服务"
  return 1
}

extra_ue_units() {
  local unit state rest
  while read -r unit state rest; do
    [[ "$unit" =~ ^oran-ue[0-9]+\.service$ ]] || continue
    if [[ "$state" == enabled* ]] || systemctl is-active --quiet "$unit"; then
      printf '%s\n' "${unit%.service}"
    fi
  done < <(systemctl list-unit-files --type=service --no-legend --no-pager)
}

nwdaf_qos_stats() {
  local raw
  raw=$(docker exec oai-nwdaf-database mongosh --quiet testing --eval '
    let n=0, latest=0;
    db.smf.find().forEach(d => {
      const q=d.qosmonlist || [];
      n+=q.length;
      if(q.length) latest=Math.max(latest, Number(q[q.length-1].timestamp?.low || 0));
    });
    print(n+" "+latest);' 2>/dev/null) || { echo "0 0"; return; }
  if [[ "$raw" =~ ^[0-9]+\ [0-9]+$ ]]; then printf '%s\n' "$raw"; else echo "0 0"; fi
}

nwdaf_progress() {
  local before=$1 before_ts=$2 after=$3 after_ts=$4 now
  now=$(date +%s)
  # Timestamp progress also handles a capped/rotated QoS history array.
  (( after > before || (after_ts > before_ts && now >= after_ts && now-after_ts <= 60) ))
}

log_count() {
  local value
  value=$(grep -c "ind_msg latency" "$1" 2>/dev/null || true)
  printf '%s\n' "${value:-0}"
}
# END STARTUP HELPERS

RAN_UNITS="flexric-ric oran-cu-cp oran-cu-up oran-du oran-ue oran-traffic flexric-xapp-kpm flexric-xapp-gtp-mac"
mapfile -t EXTRA_UE_UNITS < <(extra_ue_units)
RAN_UNITS+=" ${EXTRA_UE_UNITS[*]}"
pause_automation || exit 1

healthy_count() { docker ps --filter "health=healthy" --format '{{.Names}}' | wc -l; }
# 只看 5GC 那 8 个 NF（全机健康数会被 15 个 NWDAF 容器灌水，判不准）
cn5g_healthy() {
  docker ps --filter "health=healthy" --format '{{.Names}}' \
    | grep -cxE "oai-nrf|oai-amf|oai-smf|vpp-upf|oai-udr|oai-udm|oai-ausf|mysql"
}

# SMF 的 N4 是否活着：直接看 PFCP 心跳应答（msg type 2），别用"有没有出现过报错"来判。
# 历史报错会一直留在日志里 —— 早先版本用 tail/全文 grep，刚修好的环境会被判成"还是坏的"，
# 于是白跑一次 UPF 重建（每次重建都会踢掉现有会话）。
n4_ok() {
  local t
  t=$(docker logs --since 60s oai-smf 2>&1) || return 1
  [[ "$t" == *"handle_receive_pfcp_msg msg type 2"* ]] || return 1
  [[ "$t" == *"No UPF available"* ]] && return 1
  return 0
}

wait_n4() { # wait_n4 <秒>
  local i
  for i in $(seq 1 $(($1 / 2))); do n4_ok && return 0; sleep 2; done
  return 1
}

step "1/5 停掉 O-RAN 宿主服务（先归零）"
$SUDO systemctl stop $RAN_UNITS >/dev/null 2>&1 || { warn "停止 RAN 服务失败"; exit 1; }
ok "已停"

step "2/5 意图配置同步 + 5GC 体检"
# 意图改过的 compose env（UPF worker、N6 出口）和意图建出来的辅助容器（第二个 DN），
# 必须在 compose up 之前按回期望值，否则重建出来的容器还是旧配置。
if [ -x /usr/bin/python3 ] && [ -f /home/ubuntu/intent-driven/tools/apply_state.py ]; then
  sync_out=$(python3 /home/ubuntu/intent-driven/tools/apply_state.py 2>&1)
  echo "$sync_out" | grep -E "^\s+\[cfg\]|^applied|^unchanged" | sed 's/^/  /' || true
  ok "意图持久层已同步（DB + 配置）"
fi
if [ "$MODE" = quick ]; then
  ok "跳过（--quick）"
elif [ "$MODE" = smart ] && [ "$(cn5g_healthy)" -ge 8 ] && wait_n4 12; then
  ok "5GC 的 8 个 NF 全健康、N4 关联在 → 不动 5GC"
else
  if [ "$MODE" = force ]; then
    echo "  --force：全量冷重建"
    ( cd "$ROOT/anlf" && docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml down ) >/dev/null 2>&1
    ( cd "$ROOT/mtlf" && docker compose down ) >/dev/null 2>&1
    ( cd "$CN5G_DIR" && docker compose -f "$CN5G" down ) >/dev/null 2>&1
    ( cd "$CN5G_DIR" && docker compose -f "$CN5G" up -d ) >/dev/null 2>&1
    for i in $(seq 1 40); do [ "$(cn5g_healthy)" -ge 8 ] && break; sleep 3; done
  else
    echo "  5GC 不健康或 N4 断了 → 先轻量修（recreate vpp-upf + 重启 SMF）"
    ( cd "$CN5G_DIR" && docker compose -f "$CN5G" up -d --force-recreate --no-deps vpp-upf ) >/dev/null 2>&1
    sleep 25
    docker restart oai-smf >/dev/null 2>&1
    sleep 25
    if ! wait_n4 30; then
      warn "轻量修没成 → 全量冷重建"
      ( cd "$ROOT/anlf" && docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml down ) >/dev/null 2>&1
      ( cd "$ROOT/mtlf" && docker compose down ) >/dev/null 2>&1
      ( cd "$CN5G_DIR" && docker compose -f "$CN5G" down ) >/dev/null 2>&1
      ( cd "$CN5G_DIR" && docker compose -f "$CN5G" up -d ) >/dev/null 2>&1
      for i in $(seq 1 40); do [ "$(cn5g_healthy)" -ge 8 ] && break; sleep 3; done
      wait_n4 60
    fi
  fi
  ok "5GC 健康 NF $(cn5g_healthy)/8"
  if wait_n4 60; then ok "SMF ↔ UPF 的 N4 关联已建立"; else warn "N4 仍不通，PDU 会话会失败（docker logs oai-smf）"; fi
fi

step "3/5 AnLF/MTLF + SBI"
( cd "$ROOT/anlf" && docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml up -d ) >/dev/null 2>&1 \
  && ok "AnLF up (http2 profile)" || warn "AnLF compose 失败"
( cd "$ROOT/mtlf" && docker compose up -d ) >/dev/null 2>&1 && ok "MTLF up" || warn "MTLF compose 失败"
docker restart oai-nwdaf-sbi >/dev/null 2>&1 && ok "SBI 重启（重新订阅 SMF 事件）"

step "4/5 O-RAN 服务（按接口依赖顺序）"
for svc in flexric-ric oran-cu-cp oran-cu-up oran-du oran-ue; do
  $SUDO systemctl restart "$svc" >/dev/null 2>&1 || { warn "$svc 启动失败"; exit 1; }
  printf "  %-22s %s\n" "$svc" "$(systemctl is-active "$svc")"
  case "$svc" in
    flexric-ric) sleep 6 ;;
    oran-cu-cp)  sleep 10 ;;
    oran-cu-up)  sleep 6 ;;
    oran-du)     sleep 12 ;;
    oran-ue)     sleep 30 ;;
  esac
done
for svc in "${EXTRA_UE_UNITS[@]}"; do
  $SUDO systemctl restart "$svc" >/dev/null 2>&1 || { warn "$svc 启动失败"; exit 1; }
  printf "  %-22s %s\n" "$svc" "$(systemctl is-active "$svc")"
done
$SUDO systemctl restart oran-traffic flexric-xapp-kpm flexric-xapp-gtp-mac >/dev/null 2>&1 || { warn "traffic/xApp 启动失败"; exit 1; }
ok "traffic + 两个 xApp 已起"

step "5/5 自检"
for pat in "38412:N2" "38462:E1" "38472:F1-C"; do
  port=${pat%%:*}; name=${pat##*:}
  ss -a -A sctp 2>/dev/null | grep "ESTAB.*:$port" >/dev/null && ok "$name ESTAB" || warn "$name 未建立"
done

ue_ok() { ip -4 addr show oaitun_ue1 2>/dev/null | grep "inet 12.1.1" >/dev/null && ping -c 2 -W 2 -I oaitun_ue1 192.168.73.135 >/dev/null 2>&1; }

if ue_ok; then
  ok "UE 会话 + 用户面正常 ($(ip -4 addr show oaitun_ue1 | awk '/inet/{print $2}'))"
else
  warn "UE 无会话/用户面不通 → 交给 ue-repair.sh 走阶梯（L1 重启 UE，L2 重建 UPF+SMF）"
  UE_REPAIR_INSIDE=1 bash "$ROOT/ue-repair.sh"
  if ue_ok; then ok "自愈成功 ($(ip -4 addr show oaitun_ue1 | awk '/inet/{print $2}'))"
  else warn "自愈没成，看 $LOGS/ue-repair.log、$LOGS/ue.log、docker logs oai-smf"; fi
fi

read -r n1 ts1 < <(nwdaf_qos_stats)
sleep 20
read -r n2 ts2 < <(nwdaf_qos_stats)
if nwdaf_progress "$n1" "$ts1" "$n2" "$ts2"; then
  ok "NWDAF 全部订户的 QoS 数据在更新 ($n1 → $n2)"
else
  warn "QoS 总量和最新时间均无进展 ($n1 → $n2) → 重启 SBI 重订阅"
  docker restart oai-nwdaf-sbi >/dev/null 2>&1; sleep 25
  read -r n3 ts3 < <(nwdaf_qos_stats)
  nwdaf_progress "$n2" "$ts2" "$n3" "$ts3" && ok "重订阅后开始更新 ($n2 → $n3)" || warn "仍未更新：检查 SMF/SBI 订阅和活跃 UE"
fi

k=$(log_count "$LOGS/xapp_kpm.log")
sleep 8
k2=$(log_count "$LOGS/xapp_kpm.log")
[ "${k2:-0}" -gt "${k:-0}" ] && ok "KPM 上报中 ($k → $k2)" || warn "KPM 无新增（$LOGS/xapp_kpm.log）"

dep=$(curl -s --max-time 5 http://localhost:8000/deployments 2>/dev/null | grep -c '"status":"ready"' || true)
[ "${dep:-0}" -ge 1 ] && ok "AI Fabric 有 $dep 个 ready 部署" || warn "无 ready 部署（watchdog 2 分钟内补）"

curl -s --max-time 4 http://localhost:8989/health >/dev/null 2>&1 && ok "闭环 Flask :8989 在跑" || warn "闭环 Flask 没响应（systemctl status nwdaf-loop-netperf）"

IDS=$(docker ps --format '{{.Names}}' | grep -vx -e 'gnbsim-vpp' -e 'nwdaf-loop-netperf' | tr '\n' ' ')
[ -n "$IDS" ] && docker update --restart=unless-stopped $IDS >/dev/null 2>&1

echo
restore_automation
echo "== 完成 =="
echo "  RAN 服务: $(systemctl is-active $RAN_UNITS 2>/dev/null | paste -sd' ' -)"
echo "  容器: $(docker ps -q | wc -l) 个在跑"
echo "  守护: $(systemctl is-active oran-ue-watchdog.timer xapp-kpm-watchdog.timer oai-nf-watchdog.timer nwdaf-deployment-watchdog.timer 2>/dev/null | paste -sd' ' -)"
