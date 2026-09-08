import base64
import io

from PIL import Image

from gaia.core.images import MAX_EDGE, downscale


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_large_images_are_downscaled():
    media_type, data = downscale(_jpeg(5000, 3000))
    assert media_type == "image/jpeg"
    out = Image.open(io.BytesIO(base64.b64decode(data)))
    assert max(out.size) == MAX_EDGE


def test_small_images_are_left_alone():
    _, data = downscale(_jpeg(800, 600))
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == (800, 600)
