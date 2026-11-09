#!/usr/bin/env python3
"""
Apply CORS policy to the Cloudflare R2 bucket so browsers can fetch
PNGs and JSON files directly (Mapbox image layers, manifest fetch).

Run once:
    python backend/set_r2_cors.py
"""

import boto3
from botocore.config import Config

ACCOUNT_ID        = "52f04a53ad8a93ac402a9fc7e4121987"
ACCESS_KEY_ID     = "9fbd83b05f8049acf7f2723e6c21a1f6"
SECRET_ACCESS_KEY = "44f3d6d74bc374f1d6666774000e707a67193bb0bc7a3e27ba0626823e271e83"
BUCKET_NAME       = "tashkent-uhi"

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", region_name="auto"),
)

cors_config = {
    "CORSRules": [
        {
            # Allow any origin to GET/HEAD files — needed for:
            #   - Mapbox image source (PNG overlays)
            #   - Browser fetch of manifest JSON
            #   - TiTiler HTTP Range Requests against COG files
            "AllowedOrigins": ["*"],
            "AllowedMethods": ["GET", "HEAD"],
            "AllowedHeaders": ["*"],
            # Expose Range response headers so COG byte-range reads work
            "ExposeHeaders": [
                "Accept-Ranges",
                "Content-Range",
                "Content-Length",
                "Content-Type",
                "ETag",
            ],
            "MaxAgeSeconds": 86400,
        }
    ]
}

s3.put_bucket_cors(Bucket=BUCKET_NAME, CORSConfiguration=cors_config)
print(f"✓ CORS policy applied to bucket '{BUCKET_NAME}'")

# Verify
result = s3.get_bucket_cors(Bucket=BUCKET_NAME)
print("\nActive CORS rules:")
for rule in result["CORSRules"]:
    print(f"  Origins : {rule['AllowedOrigins']}")
    print(f"  Methods : {rule['AllowedMethods']}")
    print(f"  Exposes : {rule.get('ExposeHeaders', [])}")
    print(f"  MaxAge  : {rule.get('MaxAgeSeconds', 0)}s")
