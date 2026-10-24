import io
import secrets
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
GPS_IFD = 0x8825

# a 1280px photo is ~1.6 MP; anything near this limit is a decompression bomb
Image.MAX_IMAGE_PIXELS = 50_000_000


class BadImage(ValueError):
    pass


def _to_degrees(value, ref) -> float:
    d, m, s = (float(x) for x in value)
    deg = d + m / 60 + s / 3600
    return -deg if ref in ("S", "W") else deg


def read_gps(img: Image.Image) -> tuple[float, float] | None:
    try:
        gps = img.getexif().get_ifd(GPS_IFD)
        if not gps or 2 not in gps or 4 not in gps:
            return None
        lat = _to_degrees(gps[2], gps.get(1, "N"))
        lng = _to_degrees(gps[4], gps.get(3, "E"))
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return round(lat, 6), round(lng, 6)


def process_upload(data: bytes, upload_dir: Path, max_bytes: int, max_side: int) -> dict:
    """Validate an uploaded photo, pull its GPS, and save a clean copy.

    The saved file is re-encoded as JPEG with no EXIF at all, so the
    reporter's exact location and phone details never sit on disk.
    """
    if not data:
        raise BadImage("Empty file")
    if len(data) > max_bytes:
        raise BadImage("Photo is too large (max 8 MB)")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            probe = Image.open(io.BytesIO(data))
            fmt = probe.format
            probe.verify()
            img = Image.open(io.BytesIO(data))
            img.load()
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError, SyntaxError):
            raise BadImage("File is not a valid image")

    if fmt not in ALLOWED_FORMATS:
        raise BadImage("Only JPEG, PNG or WEBP photos are supported")

    gps = read_gps(img)

    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))

    upload_dir.mkdir(parents=True, exist_ok=True)
    image_id = secrets.token_hex(12)
    path = upload_dir / f"{image_id}.jpg"
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    path.write_bytes(out.getvalue())

    return {
        "id": image_id,
        "path": str(path),
        "lat": gps[0] if gps else None,
        "lng": gps[1] if gps else None,
    }
