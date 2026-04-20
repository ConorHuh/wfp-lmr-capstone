from functools import lru_cache

import boto3
from botocore.config import Config


@lru_cache(maxsize=4)
def get_s3_client(region: str = "us-east-1"):
    return boto3.client("s3", region_name=region, config=Config(retries={"max_attempts": 3}))
