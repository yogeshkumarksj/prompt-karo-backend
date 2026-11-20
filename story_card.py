"""Instagram story cards drawn with Pillow. No model calls and no paid APIs."""

import io
import os
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont, ImageOps

STORY_SIZE = (1080, 1920)
PREVIEW_SIZE = (270, 480)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CAPTION = 80
MAX_TITLE = 200
MAX_ARTIST = 200
COVER_TIMEOUT = 5.0
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 3600

FOOTER = "antimprompt.com"

RGB = Tuple[int, int, int]

_FONT_PATHS = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ),
}
_fonts: Dict[Tuple[bool, int], ImageFont.ImageFont] = {}
_previews: Dict[str, bytes] = {}

TEMPLATES: List[dict] = [
    {
        "id": "neon",
        "name": "Neon",
        "top": (12, 0, 32),
        "bottom": (36, 0, 64),
        "frame": (0, 255, 214),
        "title": (255, 47, 146),
        "artist": (186, 255, 250),
        "caption": (236, 236, 245),
        "muted": (150, 150, 175),
        "frame_width": 6,
        "frames": 1,
        "glow": True,
    },
    {
        "id": "bollywood_gold",
        "name": "Bollywood Gold",
        "top": (46, 6, 10),
        "bottom": (112, 40, 8),
        "frame": (232, 188, 74),
        "title": (255, 216, 110),
        "artist": (255, 236, 204),
        "caption": (255, 226, 186),
        "muted": (214, 176, 112),
        "frame_width": 8,
        "frames": 2,
        "glow": False,
    },
    {
        "id": "minimal_dark",
        "name": "Minimal Dark",
        "top": (14, 14, 16),
        "bottom": (14, 14, 16),
        "frame": (232, 232, 232),
        "title": (255, 255, 255),
        "artist": (186, 186, 186),
        "caption": (210, 210, 210),
        "muted": (128, 128, 128),
        "frame_width": 2,
        "frames": 1,
        "glow": False,
    },
    {
        "id": "sunset_gradient",
        "name": "Sunset Gradient",
        "top": (255, 112, 61),
        "bottom": (88, 22, 138),
        "frame": (255, 224, 186),
        "title": (255, 255, 255),
        "artist": (255, 232, 214),
        "caption": (255, 246, 238),
        "muted": (255, 220, 200),
        "frame_width": 5,
        "frames": 1,
        "glow": False,
    },
]
TEMPLATE_BY_ID = {item["id"]: item for item in TEMPLATES}

router = APIRouter()


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


RATE_LIMITER = SlidingWindowRateLimiter(RATE_LIMIT, RATE_WINDOW_SECONDS)


def reset_for_tests() -> None:
    RATE_LIMITER.reset()
    RATE_LIMITER.max_calls = RATE_LIMIT


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:200] or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    size = max(8, int(size))
    key = (bold, size)
    cached = _fonts.get(key)
    if cached is not None:
        return cached
    for path in _FONT_PATHS[bold]:
        if os.path.isfile(path):
            face = ImageFont.truetype(path, size)
            _fonts[key] = face
            return face
    for path in _FONT_PATHS[False]:
        if os.path.isfile(path):
            face = ImageFont.truetype(path, size)
            _fonts[key] = face
            return face
    face = ImageFont.load_default()
    _fonts[key] = face
    return face


def _gradient(size: Tuple[int, int], top: RGB, bottom: RGB) -> Image.Image:
    width, height = size
    column = Image.new("RGB", (1, height))
    span = max(1, height - 1)
    pixels = []
    for index in range(height):
        blend = index / span
        pixels.append(tuple(int(start + (end - start) * blend) for start, end in zip(top, bottom)))
    column.putdata(pixels)
    return column.resize((width, height))


def _background(spec: dict, size: Tuple[int, int]) -> Image.Image:
    image = _gradient(size, spec["top"], spec["bottom"])
    if not spec["glow"]:
        return image
    width, height = size
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse(
        (int(-0.15 * width), int(0.08 * height), int(0.72 * width), int(0.48 * height)),
        fill=(170, 0, 255, 80),
    )
    draw.ellipse(
        (int(0.32 * width), int(0.42 * height), int(1.12 * width), int(0.86 * height)),
        fill=(0, 210, 220, 55),
    )
    return Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")


def _open_image(raw: bytes, label: str) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(raw))
        kind = (image.format or "").upper()
        image = ImageOps.exif_transpose(image)
        image.load()
        image.format = kind
    except Exception:
        raise HTTPException(status_code=400, detail=f"{label} is not a valid image")
    if image.width * image.height > 24_000_000:
        raise HTTPException(status_code=400, detail=f"{label} is too large")
    return image


def _center_square(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGBA")
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    cropped = image.crop((left, top, left + side, top + side))
    return cropped.resize((size, size), Image.Resampling.LANCZOS)


def _placeholder(size: int, color: RGB) -> Image.Image:
    image = Image.new("RGBA", (size, size), (28, 28, 32, 255))
    draw = ImageDraw.Draw(image)
    stem = max(2, size // 28)
    x = int(size * 0.58)
    draw.line((x, int(size * 0.28), x, int(size * 0.68)), fill=color, width=stem)
    draw.ellipse(
        (int(size * 0.32), int(size * 0.58), int(size * 0.62), int(size * 0.78)),
        fill=color,
    )
    return image


def _rounded(image: Image.Image, radius: int) -> Image.Image:
    image = image.convert("RGBA")
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.size[0], image.size[1]), radius=radius, fill=255)
    image.putalpha(mask)
    return image


def _circle(image: Image.Image, size: int) -> Image.Image:
    squared = _center_square(image, size)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    squared.putalpha(mask)
    return squared


def _fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> List[str]:
    words = text.split()
    if not words:
        return []
    lines: List[str] = []
    current = ""
    overflow = False
    for word in words:
        trial = word if not current else f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
            current = ""
            if len(lines) == max_lines:
                overflow = True
                break
        if draw.textlength(word, font=font) <= max_width:
            current = word
            continue
        lines.append(_ellipsize(draw, word, font, max_width))
        if len(lines) == max_lines:
            overflow = True
            break
    if current:
        if len(lines) < max_lines:
            lines.append(current)
        else:
            overflow = True
    if overflow and lines and not lines[-1].endswith("…"):
        lines[-1] = _ellipsize(draw, lines[-1], font, max_width)
    return lines


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    keep = text
    while keep and draw.textlength(keep + ellipsis, font=font) > max_width:
        keep = keep[:-1]
    return (keep.rstrip() + ellipsis) if keep else ellipsis


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    font: ImageFont.ImageFont,
    y: int,
    fill: RGB,
    width: int,
    gap: int,
) -> int:
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw.text(((width - line_width) / 2, y), line, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), line, font=font)
        y += (bbox[3] - bbox[1]) + gap
    return y


def render_png(
    template_id: str,
    song_title: str,
    artist: str,
    caption: str,
    cover: Optional[Image.Image],
    photo: Optional[Image.Image],
    size: Tuple[int, int] = STORY_SIZE,
) -> bytes:
    spec = TEMPLATE_BY_ID[template_id]
    width, height = size
    scale = width / STORY_SIZE[0]
    image = _background(spec, size)
    draw = ImageDraw.Draw(image)

    inset = max(4, int(40 * scale))
    radius = max(4, int(32 * scale))
    stroke = max(1, int(spec["frame_width"] * scale))
    for index in range(spec["frames"]):
        pad = inset + index * max(4, int(14 * scale))
        draw.rounded_rectangle(
            (pad, pad, width - 1 - pad, height - 1 - pad),
            radius=radius,
            outline=spec["frame"],
            width=stroke,
        )

    cover_size = max(32, int(620 * scale))
    cover_x = (width - cover_size) // 2
    cover_y = max(8, int(280 * scale))
    art = _center_square(cover, cover_size) if cover is not None else _placeholder(cover_size, spec["frame"])
    art = _rounded(art, max(4, int(28 * scale)))
    image.paste(art, (cover_x, cover_y), art)

    text_y = cover_y + cover_size + int(48 * scale)
    if photo is not None:
        photo_size = max(24, int(250 * scale))
        circle = _circle(photo, photo_size)
        photo_x = cover_x + cover_size - int(photo_size * 0.70)
        photo_y = cover_y + cover_size - int(photo_size * 0.62)
        ring = max(2, int(6 * scale))
        draw.ellipse(
            (photo_x - ring, photo_y - ring, photo_x + photo_size + ring, photo_y + photo_size + ring),
            fill=spec["frame"],
        )
        image.paste(circle, (photo_x, photo_y), circle)
        text_y = max(text_y, photo_y + photo_size + int(28 * scale))

    draw = ImageDraw.Draw(image)
    text_width = width - int(140 * scale)
    title_font = _font(64 * scale, bold=True)
    artist_font = _font(36 * scale, bold=False)
    caption_font = _font(28 * scale, bold=False)
    footer_font = _font(22 * scale, bold=False)
    gap = max(2, int(10 * scale))

    text_y = _draw_centered(
        draw,
        _fit_lines(draw, song_title, title_font, text_width, 2),
        title_font,
        text_y,
        spec["title"],
        width,
        gap,
    )
    text_y += int(8 * scale)
    text_y = _draw_centered(
        draw,
        _fit_lines(draw, artist, artist_font, text_width, 1),
        artist_font,
        text_y,
        spec["artist"],
        width,
        gap,
    )
    if caption:
        text_y += int(18 * scale)
        _draw_centered(
            draw,
            _fit_lines(draw, caption, caption_font, text_width, 2),
            caption_font,
            text_y,
            spec["caption"],
            width,
            gap,
        )

    footer_width = draw.textlength(FOOTER, font=footer_font)
    draw.text(
        ((width - footer_width) / 2, height - int(88 * scale)),
        FOOTER,
        font=footer_font,
        fill=spec["muted"],
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _clean(value: Optional[str], limit: int, field: str, required: bool) -> str:
    text = " ".join((value or "").split())
    if required and not text:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    if len(text) > limit:
        raise HTTPException(status_code=400, detail=f"{field} must be at most {limit} characters")
    return text


def _validate_cover_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="cover_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="cover_url must be an http(s) image URL")
    host = parsed.hostname.lower().rstrip(".")
    if host != "mzstatic.com" and not host.endswith(".mzstatic.com"):
        raise HTTPException(status_code=400, detail="cover_url host is not allowed")
    if parsed.port not in (None, 80, 443):
        raise HTTPException(status_code=400, detail="cover_url host is not allowed")
    return urllib.parse.urlunsplit(parsed)


def _content_type(header: str) -> str:
    return (header or "").split(";")[0].strip().lower()


async def _fetch_cover(url: str) -> Image.Image:
    current = _validate_cover_url(url)
    try:
        async with httpx.AsyncClient(timeout=COVER_TIMEOUT, follow_redirects=False) as client:
            for _ in range(3):
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(status_code=400, detail="cover_url could not be fetched")
                        current = _validate_cover_url(urllib.parse.urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise HTTPException(status_code=400, detail="cover_url could not be fetched")
                    ctype = _content_type(response.headers.get("content-type", ""))
                    if not ctype.startswith("image/") or "svg" in ctype:
                        raise HTTPException(status_code=400, detail="cover_url must be an image")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                        raise HTTPException(status_code=400, detail="cover_url image is larger than 5MB")
                    chunks = []
                    total = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise HTTPException(status_code=400, detail="cover_url image is larger than 5MB")
                        chunks.append(chunk)
                    return _open_image(b"".join(chunks), "cover_url")
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError):
        raise HTTPException(status_code=400, detail="cover_url could not be fetched")
    raise HTTPException(status_code=400, detail="cover_url could not be fetched")


async def _read_photo(photo: Optional[UploadFile]) -> Optional[Image.Image]:
    if photo is None or not (photo.filename or "").strip():
        return None
    ctype = _content_type(photo.content_type or "")
    allowed = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp", ""}
    if ctype not in allowed and ctype != "application/octet-stream":
        raise HTTPException(status_code=400, detail="photo must be jpg, png, or webp")
    raw = await photo.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="photo is larger than 5MB")
    if not raw:
        raise HTTPException(status_code=400, detail="photo is empty")
    image = _open_image(raw, "photo")
    if (image.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
        raise HTTPException(status_code=400, detail="photo must be jpg, png, or webp")
    return image


def _preview(template_id: str) -> bytes:
    cached = _previews.get(template_id)
    if cached is not None:
        return cached
    png = render_png(template_id, "Song Title", "Artist", "", None, None, PREVIEW_SIZE)
    _previews[template_id] = png
    return png


@router.get("/ai-magic/story-card/templates")
def list_templates():
    return {
        "templates": [
            {
                "id": item["id"],
                "name": item["name"],
                "preview_url": f"/ai-magic/story-card/templates/{item['id']}/preview.png",
            }
            for item in TEMPLATES
        ]
    }


@router.get("/ai-magic/story-card/templates/{template_id}/preview.png")
def template_preview(template_id: str):
    if template_id not in TEMPLATE_BY_ID:
        raise HTTPException(status_code=404, detail="Unknown template")
    return Response(
        content=_preview(template_id),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/ai-magic/story-card")
async def create_story_card(
    request: Request,
    template_id: Optional[str] = Form(None),
    song_title: Optional[str] = Form(None),
    artist: Optional[str] = Form(None),
    cover_url: Optional[str] = Form(None),
    caption: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
):
    if not RATE_LIMITER.allow(f"story-card:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    chosen = (template_id or "").strip()
    if chosen not in TEMPLATE_BY_ID:
        raise HTTPException(status_code=400, detail="template_id is not a known template")
    title = _clean(song_title, MAX_TITLE, "song_title", required=True)
    artist_name = _clean(artist, MAX_ARTIST, "artist", required=True)
    caption_text = _clean(caption, MAX_CAPTION, "caption", required=False)
    picture = await _read_photo(photo)
    cover = await _fetch_cover(cover_url) if (cover_url or "").strip() else None
    png = render_png(chosen, title, artist_name, caption_text, cover, picture, STORY_SIZE)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
