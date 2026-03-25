import boto3
import os
import uuid
from dotenv import load_dotenv

load_dotenv()

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{os.getenv('CLOUDFLARE_R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
    aws_access_key_id=os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY"),
    region_name="auto",
)

BUCKET = os.getenv("CLOUDFLARE_R2_BUCKET_NAME")
PUBLIC_URL = os.getenv("CLOUDFLARE_R2_PUBLIC_URL")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10MB
MAX_VIDEO_SIZE = 200 * 1024 * 1024  # 200MB


def upload_file(file_bytes: bytes, content_type: str, folder: str) -> str:
    """Upload a file to R2 and return its public URL."""
    ext = content_type.split("/")[-1]
    # Normalise quicktime → mov
    if ext == "quicktime":
        ext = "mov"

    key = f"{folder}/{uuid.uuid4()}.{ext}"

    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )

    return f"{PUBLIC_URL}/{key}"


def delete_file(url: str):
    """Delete a file from R2 by its public URL."""
    key = url.replace(f"{PUBLIC_URL}/", "")
    s3.delete_object(Bucket=BUCKET, Key=key)