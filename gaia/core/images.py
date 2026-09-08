import base64
import io

from PIL import Image

MAX_EDGE = 2576  # the model's high-resolution ceiling; more pixels cost tokens
                 # and buy nothing


def downscale(raw: bytes) -> tuple[str, str]:
    """Return (media_type, base64) sized for the model."""
    image = Image.open(io.BytesIO(raw))
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
