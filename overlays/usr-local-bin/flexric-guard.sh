#!/bin/bash
# flexric-guard —— 管住 FlexRIC 的 sqlite 库与日志无界增长
# 由 flexric-guard.timer 每 15 分钟调用一次;支持 --dry-run 与目录覆盖便于测试
set -u

DB_DIR=${DB_DIR:-/tmp}
LOG_DIR=${LOG_DIR:-/home/ubuntu/oai-ran-logs}
SELF_LOG=${SELF_LOG:-/var/log/flexric-guard.log}
MAX_DB_MB=${MAX_DB_MB:-1024}      # 单个活动库超过此大小就重启对应 xApp
MIN_FREE_GB=${MIN_FREE_GB:-20}    # 根分区剩余低于此值就重启两个 xApp
MAX_LOG_MB=${MAX_LOG_MB:-64}      # 日志超过此大小截断
KEEP_TAIL_MB=${KEEP_TAIL_MB:-2}   # 截断时保留尾部
OLD_DB_MIN=${OLD_DB_MIN:-10}      # 已关闭的库超过多少分钟就删
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() {
  local msg="$(date '+%F %T') $*"
  [ "$DRY" -eq 1 ] && echo "$msg"
  mkdir -p "$(dirname "$SELF_LOG")" 2>/dev/null
  printf '%s\n' "$msg" >> "$SELF_LOG" 2>/dev/null
}

unit_for() {
  case "$1" in
    xapp_kpm_moni)      echo flexric-xapp-kpm.service ;;
    xapp_gtp_mac_rl)    echo flexric-xapp-gtp-mac.service ;;
    *)                  echo "" ;;
  esac
}

# ---- 1. 找出活动库,超限则重启对应 unit(重启会生成新库,旧库下一轮被回收) ----
declare -A LIVE
for p in $(pgrep -x xapp_kpm_moni) $(pgrep -x xapp_gtp_mac_rl); do
  comm=$(cat /proc/$p/comm 2>/dev/null) || continue
  for fd in /proc/$p/fd/*; do
    tgt=$(readlink "$fd" 2>/dev/null) || continue
    case "$tgt" in "$DB_DIR"/xapp_db*) ;; *) continue ;; esac
    deleted=0
    case "$tgt" in *"(deleted)") deleted=1 ;; esac
    path=${tgt% (deleted)}
    case "$path" in *-wal|*-shm) continue ;; esac
    LIVE[$path]=1

    if [ "$deleted" -eq 1 ]; then
      log "WARN $path 已被删除但仍被 $comm($p) 持有,数据不可见 -> 重启恢复"
      u=$(unit_for "$comm")
      [ -n "$u" ] && [ "$DRY" -eq 0 ] && systemctl restart "$u"
      continue
    fi

    szmb=$(( $(stat -c%s "$path" 2>/dev/null || echo 0) / 1048576 ))
    if [ "$szmb" -ge "$MAX_DB_MB" ]; then
      u=$(unit_for "$comm")
      log "ROTATE $path ${szmb}MB >= ${MAX_DB_MB}MB -> systemctl restart $u"
      [ -n "$u" ] && [ "$DRY" -eq 0 ] && systemctl restart "$u"
    fi
  done
done

# ---- 2. 剩余空间兜底 ----
free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
free_gb=${free_gb:-999}
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  log "WARN 根分区仅剩 ${free_gb}G < ${MIN_FREE_GB}G -> 重启两个 xApp 回收库"
  [ "$DRY" -eq 0 ] && systemctl restart flexric-xapp-kpm.service flexric-xapp-gtp-mac.service
fi

# ---- 3. 回收已关闭且超龄的库(绝不碰活动库) ----
for f in "$DB_DIR"/xapp_db*; do
  [ -e "$f" ] || continue
  base=${f%-wal}; base=${base%-shm}
  [ -n "${LIVE[$base]:-}" ] && continue
  age=$(( $(date +%s) - $(stat -c%Y "$f" 2>/dev/null || date +%s) ))
  if [ "$age" -gt $((OLD_DB_MIN * 60)) ]; then
    log "RM 旧库 $f ($(( $(stat -c%s "$f" 2>/dev/null || echo 0) / 1048576 ))MB, ${age}s 前关闭)"
    [ "$DRY" -eq 0 ] && rm -f "$f"
  fi
done

# ---- 4. 日志截断(进程是 O_APPEND 打开,截断后自动从新 EOF 续写) ----
for f in "$LOG_DIR"/ric.log "$LOG_DIR"/xapp_kpm.log "$LOG_DIR"/xapp_gtp_mac.log; do
  [ -f "$f" ] || continue
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "$sz" -gt $((MAX_LOG_MB * 1048576)) ]; then
    log "TRIM $f ${sz}B -> 保留尾部 ${KEEP_TAIL_MB}MB"
    if [ "$DRY" -eq 0 ]; then
      tail -c $((KEEP_TAIL_MB * 1048576)) "$f" > "$f.tmp" 2>/dev/null && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
    fi
  fi
done

# ---- 5. 自身日志封顶 1MB ----
if [ -f "$SELF_LOG" ] && [ "$(stat -c%s "$SELF_LOG")" -gt 1048576 ]; then
  tail -c 200000 "$SELF_LOG" > "$SELF_LOG.t" && cat "$SELF_LOG.t" > "$SELF_LOG" && rm -f "$SELF_LOG.t"
fi

exit 0
