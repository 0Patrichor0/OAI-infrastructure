#!/usr/bin/env bash
# 5GC SBI 侧看门狗 —— 针对 libnghttp2_asio 段错误导致的 SMF/UDR 崩溃
#
# 背景: oai-smf / oai-udr 在 nghttp2 asio server 的 accept 完成回调里段错误
#       (nghttp2::asio_http2::server::server::start_accept / asio_server.cc:175,
#        故障指令 movdqu 0x30(%r12),%xmm1 —— 野指针读)
#       容器有 unless-stopped 会自动起来,但 **SMF 重启会丢掉内存里的 NBI 订阅**,
#       NWDAF 的 smf 集合从此停更,而 UE 侧看起来一切正常 —— 静默失效。
#
# 本脚本:
#   1. 增量记录 dmesg 里 oai_smf/oai_udr 的段错误
#   2. SMF/UDR 容器不在 → compose 拉起
#   3. StartedAt 变化(崩溃自动重启也算)→ 重启 oai-nwdaf-sbi 恢复订阅
#   4. 连续两轮看不到 SMF 通知 → 同样恢复;仍不通就只重建 oai-smf 再试
#
# 由 oai-nf-watchdog.timer 每 2 分钟调用
set -uo pipefail

ROOT=/home/ubuntu/nwdaf-rel17
CN5G_DIR=$ROOT/oai-cn5g-fed/docker-compose
CN5G_YAML=docker-compose-basic-vpp-nrf.yaml
LOG=/home/ubuntu/oai-ran-logs/oai-nf-watchdog.log
STATE=$ROOT/.run
LOCK=$STATE/oai-nf-watchdog.lock.d
STALE_SEC=${STALE_SEC:-600}        # 通知超过这么久没更新算异常
STALE_HITS=${STALE_HITS:-2}        # 连续几轮异常才动手(防误判)
COOLDOWN_SEC=${COOLDOWN_SEC:-900}  # 两次"陈旧触发"的最小间隔
VERIFY_SEC=${VERIFY_SEC:-300}      # 恢复后等待通知的时间(秒)

mkdir -p "$(dirname "$LOG")" "$STATE"
log() { local m; m="$(date '+%F %T') $*"; echo "$m"; echo "$m" >> "$LOG" 2>/dev/null || echo "$m" >> /var/tmp/oai-nf-watchdog.log; }

if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

if [ -d "$STATE/start-all.lock.d" ]; then log "start-all.sh 正在运行,跳过"; exit 0; fi

# 后台任务没有 docker 组权限时重入
if ! docker info >/dev/null 2>&1; then
  exec sg docker -c "bash '$0' $*"
fi

# ---------- 1. 段错误增量 ----------
TOTAL=$(sudo dmesg -T 2>/dev/null | grep -icE 'segfault.*(oai_smf|oai_udr)' 2>/dev/null || true)
TOTAL=${TOTAL:-0}
PREV=$(cat "$STATE/oai-nf-segfault.count" 2>/dev/null || echo 0)
if [ "$TOTAL" -gt "$PREV" ]; then
  sudo dmesg -T 2>/dev/null | grep -iE 'segfault' | grep -E 'oai_smf|oai_udr' | tail -n $((TOTAL - PREV)) \
    | while read -r line; do log "SEGFAULT 新事件: $line"; done
fi
echo "$TOTAL" > "$STATE/oai-nf-segfault.count" 2>/dev/null || log "ERROR 无法写入 segfault 计数"

# ---------- 2. 容器存活 + 启动时间变化 ----------
RESUB=0
for nf in oai-smf oai-udr; do
  st=$(docker inspect "$nf" --format '{{.State.Running}}|{{.State.StartedAt}}' 2>/dev/null)
  if [ -z "$st" ]; then
    log "WARN $nf 容器不存在 → compose up -d $nf"
    ( cd "$CN5G_DIR" && docker compose -f "$CN5G_YAML" up -d --no-deps "$nf" ) >>"$LOG" 2>&1
    RESUB=1; continue
  fi
  running=${st%%|*}; started=${st##*|}
  if [ "$running" != "true" ]; then
    log "WARN $nf 未运行 → docker start"
    if docker start "$nf" >>"$LOG" 2>&1; then RESUB=1; else log "ERROR docker start $nf 失败"; fi
    continue
  fi
  prev_started=$(cat "$STATE/$nf.started" 2>/dev/null || echo "")
  if [ -n "$prev_started" ] && [ "$prev_started" != "$started" ]; then
    log "INFO $nf 启动时间变化 $prev_started -> $started(崩溃重启或人工重启)→ 需恢复订阅"
    RESUB=1
  fi
  echo "$started" > "$STATE/$nf.started" 2>/dev/null || log "ERROR 无法写入 $STATE/$nf.started(检查属主)"
done

# ---------- 3. 最新一条 SMF 通知有多旧 ----------
newest=$(docker logs --timestamps --tail 20000 oai-nwdaf-sbi 2>&1 \
         | grep "Storing SMF notification" | tail -1 | awk '{print $1}')
if [ -n "$newest" ]; then
  ts=${newest%%.*}; ts=${ts%Z}Z
  age=$(( $(date +%s) - $(date -d "$ts" +%s 2>/dev/null || date +%s) ))
else
  age=999999
fi

# ---------- 4. 决定是否恢复订阅 ----------
NOW=$(date +%s)
LAST_RESUB=$(cat "$STATE/oai-nf-sbi.lastresub" 2>/dev/null || echo 0)
ACT=0
if [ "$RESUB" = 1 ]; then
  ACT=1; log "恢复订阅,原因: 容器重启/缺失(最新通知 ${age}s 前)"
elif [ "$age" -gt "$STALE_SEC" ]; then
  hits=$(( $(cat "$STATE/oai-nf-stale.hits" 2>/dev/null || echo 0) + 1 ))
  echo "$hits" > "$STATE/oai-nf-stale.hits"
  if [ "$hits" -ge "$STALE_HITS" ] && [ $((NOW - LAST_RESUB)) -ge "$COOLDOWN_SEC" ]; then
    ACT=1; log "恢复订阅,原因: ${age}s 无 SMF 通知(连续 ${hits} 轮)"
  else
    log "观察: ${age}s 无 SMF 通知(第 ${hits} 轮,未达阈值或冷却中)"
  fi
else
  echo 0 > "$STATE/oai-nf-stale.hits"
fi
[ "$ACT" = 0 ] && exit 0
echo 0 > "$STATE/oai-nf-stale.hits"
echo "$NOW" > "$STATE/oai-nf-sbi.lastresub" 2>/dev/null || true

verify() {
  local limit=$1 i=0
  while [ "$i" -lt "$limit" ]; do
    sleep 10; i=$((i+10))
    if docker logs --since 90s oai-nwdaf-sbi 2>&1 | grep "Storing SMF notification" >/dev/null; then
      log "订阅已恢复(等待 ${i}s)"; return 0
    fi
  done
  return 1
}

log "重启 oai-nwdaf-sbi"
docker restart oai-nwdaf-sbi >>"$LOG" 2>&1
if verify "$VERIFY_SEC"; then exit 0; fi

# 还不行就再来一次(订阅请求偶发被 SMF 忙时丢弃),但绝不动 SMF 容器:
# 重建 SMF 会连带丢掉 UE 会话,那是 ue-repair 的 L2 决策,不该由这里越权。
log "第一次未恢复,再重启一次 oai-nwdaf-sbi"
docker restart oai-nwdaf-sbi >>"$LOG" 2>&1
if verify "$VERIFY_SEC"; then exit 0; fi

if ip link show oaitun_ue1 >/dev/null 2>&1; then
  log "ERROR 两轮重启后仍无 SMF 通知(UE 会话在),需人工查 SMF↔oai-nwdaf-sbi 8080 SBI"
else
  log "WARN UE 会话不在,UE 未重新附着前不会有 SMF 通知;交由 oran-ue-watchdog 恢复后再看"
fi
exit 1
