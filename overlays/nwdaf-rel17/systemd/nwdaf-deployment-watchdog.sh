#!/usr/bin/env bash
# 部署自愈：AI Fabric 的 deployment 只存在内存（deployed_models 字典），
# mlops-ai-fabric 容器一重建，/deployments 就变空、闭环推理开始 404。
# 这里每 2 分钟检查一次，空了就按 .deploy-model-version 里记录的版本重新部署。
set -u
NAME=nwdaf-NETWORK_PERFORMANCE-forecaster
VER_FILE=/home/ubuntu/nwdaf-rel17/.deploy-model-version
VER=$(cat "$VER_FILE" 2>/dev/null || echo 2)

if curl -s --max-time 5 http://localhost:8000/deployments | grep -q '"deployment_name"'; then
  echo "$(date '+%F %T') deployment present"
  exit 0
fi

echo "$(date '+%F %T') no deployment -> deploying $NAME v$VER"
code=$(curl -s -o /tmp/watchdog-deploy.json -w '%{http_code}' --max-time 60 \
  -X POST http://localhost:8082/nnwdaf-mlmodelprovision/v1/subscriptions \
  -H "Content-Type: application/json" \
  -d "{\"mlEventSubscs\":[{\"mlEvent\":\"NETWORK_PERFORMANCE\",\"mlModelInfo\":{\"modelId\":\"$NAME\",\"modelVersion\":\"$VER\"}}],\"notificationURI\":\"http://localhost:8000/inference\"}")
echo "$(date '+%F %T') provision HTTP=$code"
sleep 5
curl -s --max-time 5 http://localhost:8000/deployments
echo
