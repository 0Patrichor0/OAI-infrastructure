#! /bin/bash

# cd anlf

docker build --network=host --no-cache  \
            --target oai-nwdaf-nbi-analytics  --tag oai-nwdaf-nbi-analytics:latest \
            --file components/oai-nwdaf-nbi-analytics/docker/Dockerfile.nbi-analytics \
            components/oai-nwdaf-nbi-analytics


docker build --network=host --no-cache  \
            --target oai-nwdaf-nbi-events  --tag oai-nwdaf-nbi-events:latest \
            --file components/oai-nwdaf-nbi-events/docker/Dockerfile.nbi-events \
            components/oai-nwdaf-nbi-events


docker build --network=host --no-cache  \
            --target oai-nwdaf-nbi-ml  --tag oai-nwdaf-nbi-ml:latest \
            --file components/oai-nwdaf-nbi-ml/docker/Dockerfile.nbi-ml \
            components/oai-nwdaf-nbi-ml


docker build --network=host --no-cache  \
            --target oai-nwdaf-engine  --tag oai-nwdaf-engine:latest \
            --file components/oai-nwdaf-engine/docker/Dockerfile.engine \
            components/oai-nwdaf-engine

docker build --network=host --no-cache  \
            --target oai-nwdaf-engine-ads  --tag oai-nwdaf-engine-ads:latest \
            --file components/oai-nwdaf-engine-ads/docker/Dockerfile.engine-ads \
            components/oai-nwdaf-engine-ads

docker build --network=host --no-cache  \
            --target oai-nwdaf-sbi  --tag oai-nwdaf-sbi:latest \
            --file components/oai-nwdaf-sbi/docker/Dockerfile.sbi \
            components/oai-nwdaf-sbi


docker pull mongo

docker pull kong

docker image prune --force

docker buildx prune -f

sudo sysctl net.ipv4.conf.all.forwarding=1

sudo iptables -P FORWARD ACCEPT

echo "127.1.0.1   oai-nwdaf-nbi-gateway" | sudo tee -a /etc/hosts

cd ..

cd oai-cn5g-fed/docker-compose

docker compose -f docker-compose-basic-vpp-nrf.yaml up -d

cd ../../anlf


docker compose -f docker-compose/docker-compose-nwdaf-cn-http1.yaml up -d --force-recreate

docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml up -d --force-recreate

docker pull rohankharade/gnbsim

docker image tag rohankharade/gnbsim:latest gnbsim:latest

cd ../oai-cn5g-fed

docker compose -f docker-compose/docker-compose-gnbsim-vpp.yaml up -d --force-recreate