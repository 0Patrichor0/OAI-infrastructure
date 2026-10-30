#!/usr/bin/env bash
# xApp 看门狗：E42 会话假死时进程还 active、日志却不再增长，systemd 看不出来。
# 每 2 分钟比对两个 xApp 日志里的指示计数，没增长就重启对应服务。
# 注意：flexric 的 printf 重定向到文件是块缓冲（4KB 一块），
# 一次检查窗口内计数不变也可能只是缓冲没刷；所以连续两次不增长才重启。
set -u

check() { # check <服务名> <日志> <计数关键字> <stamp文件>
  local svc=$1 log=$2 pat=$3 stamp=$4
  local cur prev miss
  cur=$(grep -c "$pat" "$log" 2>/dev/null || echo 0)
  prev=$(cat "$stamp" 2>/dev/null || echo 0)
  miss=$(cat "${stamp}.miss" 2>/dev/null || echo 0)
  echo "$cur" > "$stamp"

  if [ "$cur" -gt "$prev" ]; then
    echo 0 > "${stamp}.miss"
    echo "$(date '+%F %T') $svc 正常，累计 $cur"
    return
  fi
  miss=$((miss + 1))
  echo "$miss" > "${stamp}.miss"
  if [ "$miss" -ge 2 ]; then
    echo "$(date '+%F %T') $svc 连续 2 次无增长 ($cur) -> 重启"
    systemctl restart "$svc"
    echo 0 > "${stamp}.miss"
  else
    echo "$(date '+%F %T') $svc 本轮无增长 ($cur)，下轮再看"
  fi
}

# RIC 的 E42/E2 端口在不在；不在先救 RIC
# RIC 端口要连续确认，别一次 ss 没看到就重启 —— RIC 正在重启时会误判，
# 造成 "重启 RIC → xApp 连不上 → 又重启" 的循环（实测过）。
ric_up() { ss -ln -A sctp 2>/dev/null | grep -q "127.0.0.1:36422"; }
ric_ready=0
for _ in 1 2 3; do ric_up && { ric_ready=1; break; }; sleep 3; done

if [ "$ric_ready" = 0 ]; then
  echo "$(date '+%F %T') RIC 36422 连续 3 次未监听 -> 重启 flexric-ric + 两个 xApp"
  systemctl restart flexric-ric.service
  # 等 RIC 真正起来再拉 xApp，否则 xApp 会卡在 E2 Setup 重传
  for i in $(seq 1 30); do ric_up && break; sleep 2; done
  sleep 5
  systemctl restart flexric-xapp-kpm.service flexric-xapp-gtp-mac.service
  echo "$(date '+%F %T') RIC 就绪后已重启两个 xApp（等 $((i*2))s）"
  exit 0
fi

# RIC 在,但 RAN 侧 E2 agent 没注册上 —— 最常见于「RIC 重启过」:
# CU-CP/DU 的 E2 agent 只在启动时连一次,不会自动重连。现象是 xApp 日志反复
# "The nearRT-RIC has no registered nodes",KPM 库换文件但一行数据都没有。
# 救法:按 RIC → CU-CP → DU → UE → xApp 的顺序重启,让 E2 重新 Setup。
if awk '/no registered nodes/{bad=NR} /Successfully subscribed/{good=NR} END{exit !(bad>good)}' \
     <(tail -200 /home/ubuntu/oai-ran-logs/xapp_kpm.log 2>/dev/null); then
  echo "$(date '+%F %T') xApp 报 no registered nodes -> 重启 RAN E2 agent(cu-cp→du→ue)+xApp"
  systemctl restart oran-cu-cp.service; sleep 8
  systemctl restart oran-du.service;   sleep 12
  systemctl restart oran-ue.service;   sleep 15
  systemctl restart flexric-xapp-kpm.service flexric-xapp-gtp-mac.service
  echo 0 > /tmp/.xapp_kpm_count.miss 2>/dev/null
  echo 0 > /tmp/.xapp_gtpmac_count.miss 2>/dev/null
  exit 0
fi

check flexric-xapp-kpm     /home/ubuntu/oai-ran-logs/xapp_kpm.log     "ind_msg latency" /tmp/.xapp_kpm_count
check flexric-xapp-gtp-mac /home/ubuntu/oai-ran-logs/xapp_gtp_mac.log "ind_msg latency" /tmp/.xapp_gtpmac_count
