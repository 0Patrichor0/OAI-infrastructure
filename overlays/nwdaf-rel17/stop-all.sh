#!/usr/bin/env bash
# 按逆序停整套 NWDAF rel17（MTLF → AnLF → 5GC）。数据卷保留。
set -euo pipefail

ROOT=/home/ubuntu/nwdaf-rel17
# docker 组权限：当前 shell 不在组里就把整个脚本用 sg 重新拉起来
# （不要用 sg docker -c "长命令" 包住，多行命令会被二次解析搞坏）
if ! docker info >/dev/null 2>&1; then
  exec sg docker -c "bash '$0' $*"
fi


echo "== 1/3 MTLF =="
( cd "$ROOT/mtlf" && docker compose down )

echo "== 2/3 AnLF =="
( cd "$ROOT/anlf" && docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml down )

echo "== 3/3 5GC =="
( cd "$ROOT/oai-cn5g-fed/docker-compose" && docker compose -f docker-compose-basic-vpp-nrf.yaml down )

echo "== 剩余容器：$(docker ps -q | wc -l) 个 =="
