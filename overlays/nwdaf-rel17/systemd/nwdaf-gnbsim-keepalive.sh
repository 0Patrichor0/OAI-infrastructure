#!/usr/bin/env bash
# gnbsim（模拟 gNB+UE）保活：不在跑就用 compose 重建，保证 SMF 持续有新数据上报。
# 注意：不能用 `docker start gnbsim-vpp` —— 该容器 entrypoint 依赖全新网络命名空间，
# 直接 start 会立刻 exit 1 且不产生任何日志。
set -u
NAME=gnbsim-vpp
COMPOSE_DIR=/home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose
if [ -n "$(docker ps -q -f "name=^${NAME}$")" ]; then
  echo "$(date '+%F %T') gnbsim-vpp running"
  exit 0
fi
echo "$(date '+%F %T') gnbsim-vpp not running -> recreating via compose"
cd "$COMPOSE_DIR" || exit 1
docker compose -f docker-compose-gnbsim-vpp.yaml up -d --force-recreate 2>&1 | tail -3
sleep 15
docker ps --format '{{.Names}} {{.Status}}' | grep "^${NAME} " \
  || echo "$(date '+%F %T') recreate FAILED"
