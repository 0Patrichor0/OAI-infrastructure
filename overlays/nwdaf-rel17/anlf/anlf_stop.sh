#! /bin/bash

# cd anlf

docker compose -f docker-compose/docker-compose-nwdaf-cn-http1.yaml down

docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml down

cd ../oai-cn5g-fed

docker compose -f docker-compose/docker-compose-gnbsim-vpp.yaml down

cd docker-compose

docker compose -f docker-compose-basic-vpp-nrf.yaml down