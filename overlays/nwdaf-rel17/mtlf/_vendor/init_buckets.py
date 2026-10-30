import os, boto3
endpoint = "http://" + os.environ["MINIO_ENDPOINT"]
s3 = boto3.client("s3", endpoint_url=endpoint,
                  aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                  region_name="us-east-1")
for b in (os.environ.get("DATASETS_BUCKET", "datasets"), "mlflow"):
    try:
        s3.create_bucket(Bucket=b); print("created", b)
    except Exception as e:
        print("bucket", b, "->", type(e).__name__, str(e)[:80])
print("buckets:", [x["Name"] for x in s3.list_buckets()["Buckets"]])
