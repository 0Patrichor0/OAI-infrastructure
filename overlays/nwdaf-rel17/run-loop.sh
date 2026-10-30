#!/usr/bin/env bash
# 跑 NWDAF 闭环脚本，日志同时打到屏幕和 /home/ubuntu/nwdaf-rel17/logs/<用例>-<时间>.log
#
#   ./run-loop.sh netperf             # 流量预测，默认跑 60 秒
#   ./run-loop.sh netperf 300         # 跑 5 分钟
#   ./run-loop.sh abnormal            # 异常检测
#
# 依赖：容器 python:3.10-slim（宿主机 py3.12 装不上 numpy==1.24.3）

# docker 组权限：当前 shell 不在组里就把整个脚本用 sg 重新拉起来。
# 不要用 sg docker -c "长命令" 去包 docker run —— 里面的多行脚本会被二次解析搞坏，
# 表现是 docker run 静默失败、日志文件 0 字节。
if ! docker info >/dev/null 2>&1; then
  exec sg docker -c "bash '$0' $*"
fi

set -uo pipefail

ROOT=/home/ubuntu/nwdaf-rel17
CASE="${1:-netperf}"
SECS="${2:-60}"

case "$CASE" in
  netperf|network_performance|np) SCRIPT=network_performance/traffic_forecasting.py ;;
  abnormal|abnormal_behavior|ab)  SCRIPT=abnormal_behavior/anomaly_detection.py ;;
  *) echo "用法: $0 [netperf|abnormal] [秒数]"; exit 2 ;;
esac

mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/${CASE}-$(date +%Y%m%d-%H%M%S).log"

echo "用例: $SCRIPT  |  时长: ${SECS}s  |  日志: $LOG"
echo "（Ctrl-C 随时停；推理结果每 10 秒一条）"
echo "----------------------------------------------------------------------"

docker run --rm --network host \
  -v "$ROOT/examples:/examples:ro" \
  -e "SCRIPT=$SCRIPT" -e "SECS=$SECS" \
  python:3.10-slim bash -c '
    pip install -q -r /examples/requirements.txt 2>/dev/null
    cd /examples && exec timeout "$SECS" python "$SCRIPT"
  ' 2>&1 | tee "$LOG"

echo "----------------------------------------------------------------------"
echo "结束。日志留存在 $LOG"
