#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/ubuntu/nwdaf-rel17
set -a
source "$ROOT/mtlf/.env"
set +a
docker run --rm --network oai-nwdaf-net -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY python:3.10-slim bash -c '
pip install -q boto3
python - <<PY
import boto3,os
s3=boto3.client("s3",endpoint_url="http://minio:9000",region_name="us-east-1",aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"])
for bucket in s3.list_buckets()["Buckets"]: print(bucket["Name"])
PY'
