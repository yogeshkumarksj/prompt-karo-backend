"""Share-card templates stored in Postgres. Drawing is Pillow only."""

import io
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageChops, ImageDraw
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

import models
import story_card
from database import SessionLocal, engine, get_db

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT = 80
MAX_NAME = 80
MAX_CATEGORY = 40
MAX_SIDE = 2048
RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 3600
SEED_PHOTO_BOX = (230, 280, 620, 620)
SEED_TEXT_BOX = (120, 980, 840, 240)

router = APIRouter()
_previews: Dict[str, bytes] = {}
_table_ready = False


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
    global _table_ready
    RATE_LIMITER.reset()
    RATE_LIMITER.max_calls = RATE_LIMIT
    _previews.clear()
    _table_ready = False
    _ready_table()
    db = SessionLocal()
    try:
        db.query(models.ViralTemplate).delete()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ready_table() -> None:
    global _table_ready
    if _table_ready:
        return
    models.Base.metadata.create_all(bind=engine, tables=[models.ViralTemplate.__table__])
    _table_ready = True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:200] or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    import main

    if not (main.ADMIN_KEY and main.ADMIN_KEY.strip()):
        raise HTTPException(status_code=503, detail="ADMIN_KEY is not configured")
    if not main._keys_match(x_admin_key, main.ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid Admin Key")


def _hex_color(spec_rgb: Tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*spec_rgb)


def _parse_color(value: Optional[str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return "#FFFFFF"
    if not raw.startswith("#"):
        raise HTTPException(status_code=400, detail="text_color must be a hex color")
    digits = raw[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    if len(digits) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
        raise HTTPException(status_code=400, detail="text_color must be a hex color")
    return "#" + digits.lower()


def _color_rgb(value: str) -> Tuple[int, int, int]:
    digits = value[1:]
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _parse_box(value: Optional[str], width: int, height: int, field: str) -> Tuple[int, int, int, int]:
    parts = [part.strip() for part in (value or "").split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail=f"{field} must be x,y,w,h")
    try:
        x, y, w, h = (int(part) for part in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be x,y,w,h")
    if min(w, h) <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise HTTPException(status_code=400, detail=f"{field} is outside the template")
    return x, y, w, h


def _box_text(box: Tuple[int, int, int, int]) -> str:
    return ",".join(str(part) for part in box)


def _content_type(header: str) -> str:
    return (header or "").split(";")[0].strip().lower()


def _punch(image: Image.Image, box: Tuple[int, int, int, int], radius: int) -> None:
    x, y, w, h = box
    alpha = image.getchannel("A")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    region = alpha.crop((x, y, x + w, y + h))
    alpha.paste(ImageChops.multiply(region, mask.point(lambda pixel: 255 - pixel)), (x, y))
    image.putalpha(alpha)


def _seed_overlay(spec: dict) -> bytes:
    width, height = story_card.STORY_SIZE
    image = story_card._background(spec, story_card.STORY_SIZE).convert("RGBA")
    draw = ImageDraw.Draw(image)
    inset = 40
    radius = 32
    stroke = max(1, int(spec["frame_width"]))
    for index in range(spec["frames"]):
        pad = inset + index * 14
        draw.rounded_rectangle(
            (pad, pad, width - 1 - pad, height - 1 - pad),
            radius=radius,
            outline=spec["frame"],
            width=stroke,
        )
    x, y, w, h = SEED_PHOTO_BOX
    draw.rounded_rectangle((x - 6, y - 6, x + w + 6, y + h + 6), radius=34, outline=spec["frame"], width=6)
    _punch(image, SEED_PHOTO_BOX, 28)
    draw = ImageDraw.Draw(image)
    footer_font = story_card._font(22, bold=False)
    footer = story_card.FOOTER
    footer_width = draw.textlength(footer, font=footer_font)
    draw.text(((width - footer_width) / 2, height - 88), footer, font=footer_font, fill=spec["muted"])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def ensure_seeded(db: Optional[Session] = None) -> None:
    _ready_table()
    own_session = db is None
    if own_session:
        db = SessionLocal()
    assert db is not None
    try:
        if db.query(models.ViralTemplate.id).first():
            return
        now = datetime.now(timezone.utc)
        for index, spec in enumerate(story_card.TEMPLATES):
            db.add(
                models.ViralTemplate(
                    id=spec["id"],
                    name=spec["name"],
                    category="classic",
                    width=story_card.STORY_SIZE[0],
                    height=story_card.STORY_SIZE[1],
                    photo_box=_box_text(SEED_PHOTO_BOX),
                    text_box=_box_text(SEED_TEXT_BOX),
                    text_color=_hex_color(spec["title"]),
                    overlay=_seed_overlay(spec),
                    created_at=now - timedelta(seconds=index),
                )
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    finally:
        if own_session:
            db.close()


def _public(row: models.ViralTemplate) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "category": row.category,
        "preview_url": f"/ai-magic/viral-templates/{row.id}/preview.png",
        "width": row.width,
        "height": row.height,
        "has_text": bool(row.text_box),
    }


def _load(db: Session, template_id: str) -> models.ViralTemplate:
    ensure_seeded(db)
    row = db.query(models.ViralTemplate).filter(models.ViralTemplate.id == template_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown template")
    return row


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    image = image.convert("RGBA")
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _placeholder(width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), (72, 72, 80, 255))
    draw = ImageDraw.Draw(image)
    cx, cy = width // 2, int(height * 0.40)
    radius = max(8, min(width, height) // 6)
    fill = (214, 214, 220, 255)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill)
    draw.ellipse(
        (cx - int(radius * 1.7), cy + int(radius * 1.2), cx + int(radius * 1.7), cy + int(radius * 3.2)),
        fill=fill,
    )
    return image


def _draw_text(image: Image.Image, box: Tuple[int, int, int, int], text: str, color: Tuple[int, int, int]) -> None:
    x, y, w, h = box
    draw = ImageDraw.Draw(image)
    size = max(16, min(72, h // 2))
    font = story_card._font(size, bold=True)
    max_width = max(8, w - 16)

    def block(lines, face) -> int:
        total = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=face)
            total += (bbox[3] - bbox[1]) + 4
        return total

    lines = story_card._fit_lines(draw, text, font, max_width, 3)
    while size > 16 and (block(lines, font) > h - 8 or any(draw.textlength(line, font=font) > max_width for line in lines)):
        size -= 2
        font = story_card._font(size, bold=True)
        lines = story_card._fit_lines(draw, text, font, max_width, 3)
    cursor = y + max(0, (h - block(lines, font)) // 2)
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw.text((x + (w - line_width) / 2, cursor), line, font=font, fill=color)
        bbox = draw.textbbox((0, 0), line, font=font)
        cursor += (bbox[3] - bbox[1]) + 4


def compose(row: models.ViralTemplate, photo: Image.Image, text: str) -> bytes:
    base = Image.new("RGBA", (row.width, row.height), (0, 0, 0, 255))
    px, py, pw, ph = _parse_box(row.photo_box, row.width, row.height, "photo_box")
    fitted = _cover(photo, pw, ph)
    base.paste(fitted, (px, py), fitted)
    overlay = story_card._open_image(bytes(row.overlay), "overlay").convert("RGBA")
    if overlay.size != (row.width, row.height):
        overlay = overlay.resize((row.width, row.height), Image.Resampling.LANCZOS)
    base = Image.alpha_composite(base, overlay)
    if text and row.text_box:
        _draw_text(base, _parse_box(row.text_box, row.width, row.height, "text_box"), text, _color_rgb(row.text_color or "#FFFFFF"))
    flat = Image.new("RGB", base.size, (0, 0, 0))
    flat.paste(base, mask=base.getchannel("A"))
    buffer = io.BytesIO()
    flat.save(buffer, format="PNG")
    return buffer.getvalue()


def _clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    if len(value) > MAX_TEXT:
        raise HTTPException(status_code=400, detail=f"text must be at most {MAX_TEXT} characters")
    return " ".join(value.split())


async def _read_photo(photo: Optional[UploadFile]) -> Image.Image:
    if photo is None or not (photo.filename or "").strip():
        raise HTTPException(status_code=400, detail="photo is required")
    ctype = _content_type(photo.content_type or "")
    allowed = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp", "", "application/octet-stream"}
    if ctype not in allowed:
        raise HTTPException(status_code=400, detail="photo must be jpg, png, or webp")
    raw = await photo.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="photo is larger than 5MB")
    if not raw:
        raise HTTPException(status_code=400, detail="photo is empty")
    image = story_card._open_image(raw, "photo")
    if (image.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
        raise HTTPException(status_code=400, detail="photo must be jpg, png, or webp")
    return image


async def _read_overlay(overlay: Optional[UploadFile]) -> Tuple[bytes, int, int]:
    if overlay is None or not (overlay.filename or "").strip():
        raise HTTPException(status_code=400, detail="overlay is required")
    ctype = _content_type(overlay.content_type or "")
    if ctype and ctype not in {"image/png", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="overlay must be a PNG")
    raw = await overlay.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="overlay is larger than 5MB")
    if not raw:
        raise HTTPException(status_code=400, detail="overlay is empty")
    image = story_card._open_image(raw, "overlay")
    if (image.format or "").upper() != "PNG":
        raise HTTPException(status_code=400, detail="overlay must be a PNG")
    width, height = image.size
    if max(width, height) > MAX_SIDE or min(width, height) < 1:
        raise HTTPException(status_code=400, detail="overlay size is not supported")
    return raw, width, height


@router.get("/ai-magic/viral-templates")
def list_templates(db: Session = Depends(get_db)):
    ensure_seeded(db)
    rows = (
        db.query(models.ViralTemplate)
        .options(defer(models.ViralTemplate.overlay))
        .order_by(models.ViralTemplate.created_at.desc(), models.ViralTemplate.id.desc())
        .all()
    )
    return {"templates": [_public(row) for row in rows]}


@router.get("/ai-magic/viral-templates/{template_id}/preview.png")
def template_preview(template_id: str, db: Session = Depends(get_db)):
    cached = _previews.get(template_id)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
    row = _load(db, template_id)
    png = compose(row, _placeholder(64, 64), "")
    _previews[template_id] = png
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@router.post("/ai-magic/viral-templates/{template_id}/render")
async def render_template(
    template_id: str,
    request: Request,
    photo: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not RATE_LIMITER.allow(f"viral-template:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    row = _load(db, template_id)
    caption = _clean_text(text)
    picture = await _read_photo(photo)
    png = compose(row, picture, caption)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/admin/viral-templates")
async def create_template(
    name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    photo_box: Optional[str] = Form(None),
    text_box: Optional[str] = Form(None),
    text_color: Optional[str] = Form(None),
    overlay: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    ensure_seeded(db)
    label = " ".join((name or "").split())
    if not label:
        raise HTTPException(status_code=400, detail="name is required")
    if len(label) > MAX_NAME:
        raise HTTPException(status_code=400, detail=f"name must be at most {MAX_NAME} characters")
    group = " ".join((category or "").split()) or None
    if group and len(group) > MAX_CATEGORY:
        raise HTTPException(status_code=400, detail=f"category must be at most {MAX_CATEGORY} characters")
    raw, width, height = await _read_overlay(overlay)
    photo = _parse_box(photo_box, width, height, "photo_box")
    caption_box = _parse_box(text_box, width, height, "text_box") if (text_box or "").strip() else None
    color = _parse_color(text_color)
    row = models.ViralTemplate(
        id=secrets.token_hex(8),
        name=label,
        category=group,
        width=width,
        height=height,
        photo_box=_box_text(photo),
        text_box=_box_text(caption_box) if caption_box else None,
        text_color=color,
        overlay=raw,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="could not store template")
    db.refresh(row)
    return {"template": _public(row)}


@router.delete("/admin/viral-templates/{template_id}")
def delete_template(
    template_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    ensure_seeded(db)
    row = db.query(models.ViralTemplate).filter(models.ViralTemplate.id == template_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown template")
    db.delete(row)
    db.commit()
    _previews.pop(template_id, None)
    return {"message": "Deleted"}
