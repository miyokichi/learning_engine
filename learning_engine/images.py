"""Bounded local image attachments for OpenAI-compatible vision requests."""
import base64
import hashlib
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4


def load_image(path):
    path = Path(path).expanduser()
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("画像は空でない5 MiB以下のファイルにしてください")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("画像はPNG・JPEG・WebP形式にしてください")
    return {"filename": path.name, "mime_type": mime, "data": data,
            "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def image_part(attachment):
    encoded = base64.b64encode(attachment["data"]).decode("ascii")
    return {"type": "image_url", "image_url": {
        "url": f"data:{attachment['mime_type']};base64,{encoded}"}}
