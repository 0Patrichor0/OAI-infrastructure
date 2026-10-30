#======= Quick start ============================================================
#!/bin/bash

# Move into Nwdaf repo
# cd anlf

# Start NRF stack
cd ../oai-cn5g-fed/docker-compose
docker compose -f docker-compose-basic-vpp-nrf.yaml up -d

# Start NWDAF stack
cd ../../anlf
docker compose -f docker-compose/docker-compose-nwdaf-cn-http1.yaml up -d --force-recreate
docker compose -f docker-compose/docker-compose-nwdaf-cn-http2.yaml up -d --force-recreate

# Start gnbsim
docker compose -f ../oai-cn5g-fed/docker-compose/docker-compose-gnbsim-vpp.yaml up -d --force-recreate
