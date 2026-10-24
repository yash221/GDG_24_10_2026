import io
import secrets
import warnings
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
GPS_IFD = 0x8825
EXIF_IFD = 0x8769
MAKE, MODEL, SOFTWARE, DATETIME, DATETIME_ORIGINAL = 0x010F, 0x0110, 0x0131, 0x0132, 0x9003

# names AI image tools leave in the Software tag or PNG text chunks
AI_TOOL_MARKERS = ("midjourney", "dall-e", "dall·e", "stable diffusion", "firefly", "imagen",
                   "gemini", "ideogram", "leonardo", "flux", "sdxl", "comfyui", "novelai")
AI_PNG_KEYS = ("parameters", "prompt", "workflow")

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


def read_meta(img: Image.Image) -> dict:
    """Camera and provenance hints, read before EXIF is stripped."""
    try:
        exif = img.getexif()
        sub = exif.get_ifd(EXIF_IFD)
    except Exception:
        exif, sub = {}, {}
    software = str(exif.get(SOFTWARE) or "").lower()
    taken = sub.get(DATETIME_ORIGINAL) or exif.get(DATETIME)
    taken_at = None
    if taken:
        try:
            taken_at = datetime.strptime(str(taken).strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    ai = any(m in software for m in AI_TOOL_MARKERS) or any(k in img.info for k in AI_PNG_KEYS)
    return {"camera": bool(exif.get(MAKE) or exif.get(MODEL)), "taken_at": taken_at, "ai_metadata": ai}


def image_hash(img: Image.Image) -> str:
    """64-bit difference hash: near-identical photos give near-identical hashes."""
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hash_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


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
    meta = read_meta(img)
    meta["gps"] = gps is not None

    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    meta["phash"] = image_hash(img)

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
        "meta": meta,
    }
