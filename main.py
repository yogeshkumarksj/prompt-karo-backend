import asyncio
import io
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google import genai
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

import ai_gateway
import crud
import story_card
import viral_browse
import models
import schemas
from database import engine, get_db

INSTA_LOOTER_HOST = "instagram-looter2.p.rapidapi.com"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UPLOAD_HANDLER_KEY = os.getenv("UPLOAD_HANDLER_KEY")
UPLOAD_HANDLER_URL = os.getenv(
    "UPLOAD_HANDLER_URL",
    "https://antimprompt.com/upload_handler.php",
)
HOSTINGER_PUBLIC_BASE = os.getenv("HOSTINGER_PUBLIC_BASE", "https://antimprompt.com")

# Image encode defaults: keep gallery images reasonably small for the web UI.
IMAGE_MAX_DIMENSION = int(os.getenv("IMAGE_MAX_DIMENSION", "1600"))
IMAGE_WEBP_QUALITY = int(os.getenv("IMAGE_WEBP_QUALITY", "80"))
THUMB_MAX_DIMENSION = int(os.getenv("THUMB_MAX_DIMENSION", "480"))
THUMB_WEBP_QUALITY = int(os.getenv("THUMB_WEBP_QUALITY", "75"))

app = FastAPI(title="Antim Prompt API")

# Shared async HTTP client for RapidAPI, Instagram CDN and Hostinger.
# Keeping one client avoids creating a new TCP connection for every request.
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=10.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
    follow_redirects=True,
)

# 1. CORS Settings
# IMPORTANT:
# Do not use allow_origins=["*"] together with allow_credentials=True.
# Override with CORS_ORIGINS (comma-separated) when needed.
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://antimprompt.com",
    "https://www.antimprompt.com",
]
_cors_env = os.getenv("CORS_ORIGINS") or os.getenv("ALLOWED_CORS_ORIGINS")
if _cors_env and _cors_env.strip():
    CORS_ORIGINS = [origin.strip() for origin in _cors_env.split(",") if origin.strip()]
else:
    CORS_ORIGINS = list(_DEFAULT_CORS_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 2. Local uploads folder (dev backup / static mount)
UPLOAD_ROOT = "uploads"
if not os.path.exists(UPLOAD_ROOT):
    os.makedirs(UPLOAD_ROOT)

app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")
app.include_router(story_card.router)

models.Base.metadata.create_all(bind=engine)

ADMIN_KEY = os.getenv("ADMIN_KEY")


def _keys_match(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time compare; never true for missing/blank values."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def verify_admin(x_admin_key: str = Header(None)):
    # Fail closed: a missing or blank server key must reject every request.
    if not (ADMIN_KEY and ADMIN_KEY.strip()):
        raise HTTPException(status_code=503, detail="ADMIN_KEY is not configured")
    if not _keys_match(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid Admin Key")
    return True


def require_upload_handler_key() -> str:
    if not (UPLOAD_HANDLER_KEY and UPLOAD_HANDLER_KEY.strip()):
        raise HTTPException(
            status_code=503,
            detail="UPLOAD_HANDLER_KEY is not configured",
        )
    return UPLOAD_HANDLER_KEY


class AdminVerifyRequest(BaseModel):
    key: str


class SlidingWindowRateLimiter:
    """Simple in-process sliding-window limiter (per-key)."""

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


# Public write-ish / abuse-prone endpoints stay unauthenticated but rate-limited.
INCREMENT_RATE_LIMITER = SlidingWindowRateLimiter(max_calls=60, window_seconds=60)
SETTINGS_READ_RATE_LIMITER = SlidingWindowRateLimiter(max_calls=120, window_seconds=60)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce_rate_limit(limiter: SlidingWindowRateLimiter, request: Request, label: str) -> None:
    key = f"{label}:{client_ip(request)}"
    if not limiter.allow(key):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")


def optimize_image_bytes(
    raw: bytes,
    *,
    max_dimension: int = IMAGE_MAX_DIMENSION,
    quality: int = IMAGE_WEBP_QUALITY,
    output_format: str = "WEBP",
) -> bytes:
    """Resize (long side) and re-encode; used for prompt + profile uploads."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    width, height = img.size
    longest = max(width, height)
    if longest > max_dimension > 0:
        scale = max_dimension / float(longest)
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    fmt = output_format.upper()
    save_kwargs: Dict[str, Any] = {"format": fmt, "quality": quality}
    if fmt == "WEBP":
        save_kwargs["method"] = 6
    img.save(buffer, **save_kwargs)
    return buffer.getvalue()


def upload_to_hostinger_sync(
    *,
    file_bytes: bytes,
    filename: str,
    category: str,
    content_type: str,
) -> None:
    """Blocking upload used from publish-prompt (sync requests)."""
    upload_key = require_upload_handler_key()
    response = requests.post(
        UPLOAD_HANDLER_URL,
        data={
            "key": upload_key,
            "category": category,
            "filename": filename,
        },
        files={"file": (filename, file_bytes, content_type)},
        timeout=60,
    )
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Hostinger PHP Bridge Error")


@app.get("/")
def read_root():
    return {"message": "Antim Prompt API is Live!"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/verify")
def admin_verify(payload: AdminVerifyRequest):
    """Frontend can check a typed admin key without calling a mutating route."""
    if not (ADMIN_KEY and ADMIN_KEY.strip()):
        raise HTTPException(status_code=503, detail="ADMIN_KEY is not configured")
    if not _keys_match(payload.key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid Admin Key")
    return {"valid": True}


@app.options("/ai-magic/insta-audit/{username}")
async def insta_audit_options(username: str):
    return {"status": "ok"}


# --- PUBLISH PROMPT ---
@app.post("/publish-prompt/")
async def publish_prompt(
    title: str = Form(...),
    prompt_text: str = Form(...),
    category: str = Form(...),
    tags: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin),
):
    # Fail early if the Hostinger bridge key is missing (before any DB write).
    require_upload_handler_key()
    new_prompt = None

    try:
        new_prompt = models.Prompt(
            title=title,
            prompt_text=prompt_text,
            category=category,
            tags=tags,
            image_url="",
        )
        db.add(new_prompt)
        db.commit()
        db.refresh(new_prompt)

        file_content = await file.read()
        webp_bytes = optimize_image_bytes(
            file_content,
            max_dimension=IMAGE_MAX_DIMENSION,
            quality=IMAGE_WEBP_QUALITY,
            output_format="WEBP",
        )
        thumb_bytes = optimize_image_bytes(
            file_content,
            max_dimension=THUMB_MAX_DIMENSION,
            quality=THUMB_WEBP_QUALITY,
            output_format="WEBP",
        )

        new_filename = f"{new_prompt.id}.webp"
        thumb_filename = f"{new_prompt.id}_thumb.webp"
        safe_category = category.strip().replace(" ", "_").lower()

        upload_to_hostinger_sync(
            file_bytes=webp_bytes,
            filename=new_filename,
            category=safe_category,
            content_type="image/webp",
        )
        upload_to_hostinger_sync(
            file_bytes=thumb_bytes,
            filename=thumb_filename,
            category=safe_category,
            content_type="image/webp",
        )

        final_url = f"{HOSTINGER_PUBLIC_BASE}/uploads/{safe_category}/{new_filename}"
        thumbnail_url = f"{HOSTINGER_PUBLIC_BASE}/uploads/{safe_category}/{thumb_filename}"

        new_prompt.image_url = final_url
        db.commit()
        # Keep existing `url` field; add optional thumbnail_url for newer clients.
        return {
            "status": "success",
            "url": final_url,
            "thumbnail_url": thumbnail_url,
        }

    except HTTPException:
        if new_prompt is not None and getattr(new_prompt, "id", None) is not None:
            try:
                db.delete(new_prompt)
                db.commit()
            except Exception:
                db.rollback()
        else:
            db.rollback()
        raise
    except Exception as e:
        if new_prompt is not None and getattr(new_prompt, "id", None) is not None:
            try:
                db.delete(new_prompt)
                db.commit()
            except Exception:
                db.rollback()
        else:
            db.rollback()
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- PROMPTS ---
@app.get("/prompts/", response_model=List[schemas.Prompt])
def read_prompts(search: str = None, category: str = None, sort_by: str = None, db: Session = Depends(get_db)):
    return crud.get_prompts(db, search=search, category=category, sort_by=sort_by)


@app.delete("/prompts/{prompt_id}/")
def delete_prompt(prompt_id: int, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt:
        raise HTTPException(status_code=404)
    db.delete(db_prompt)
    db.commit()
    return {"message": "Deleted"}


@app.put("/prompts/{prompt_id}/", response_model=schemas.Prompt)
def update_prompt(
    prompt_id: int,
    prompt: schemas.PromptCreate,
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin),
):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt:
        raise HTTPException(status_code=404)
    db_prompt.title = prompt.title
    db_prompt.prompt_text = prompt.prompt_text
    db_prompt.image_url = prompt.image_url
    db_prompt.category = prompt.category
    db.commit()
    db.refresh(db_prompt)
    return db_prompt


# --- ANALYTICS ---
@app.post("/prompts/{prompt_id}/increment/{action}/")
def increment_stat(
    prompt_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(INCREMENT_RATE_LIMITER, request, "increment")
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt:
        raise HTTPException(status_code=404)
    if action == "view":
        db_prompt.views += 1
    elif action == "copy":
        db_prompt.copies += 1
    elif action == "share":
        db_prompt.shares += 1
    db.commit()
    return {"status": "success"}


# --- CATEGORIES ---
@app.get("/categories/", response_model=List[schemas.Category])
def read_categories(db: Session = Depends(get_db)):
    return crud.get_categories(db)


@app.get("/categories/with-count/")
def get_categories_with_count(db: Session = Depends(get_db)):
    categories = db.query(models.Category).all()
    result = []
    for cat in categories:
        count = db.query(models.Prompt).filter(models.Prompt.category == cat.name).count()
        result.append({"id": cat.id, "name": cat.name, "prompt_count": count})
    return result


@app.post("/categories/", response_model=schemas.Category)
def create_category(
    category: schemas.CategoryCreate,
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin),
):
    return crud.create_category(db=db, category=category)


# --- ADMIN STATS ---
@app.get("/admin/stats/")
def get_admin_stats(db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    total_prompts = db.query(models.Prompt).count()
    total_views = db.query(func.sum(models.Prompt.views)).scalar() or 0
    total_copies = db.query(func.sum(models.Prompt.copies)).scalar() or 0
    total_shares = db.query(func.sum(models.Prompt.shares)).scalar() or 0
    return {
        "total_prompts": total_prompts,
        "total_views": total_views,
        "total_copies": total_copies,
        "total_shares": total_shares,
    }


# --- SETTINGS ---
@app.get("/settings/")
def get_settings(request: Request, db: Session = Depends(get_db)):
    # Public read for the UI timer; rate-limit to reduce scraping/abuse.
    enforce_rate_limit(SETTINGS_READ_RATE_LIMITER, request, "settings")
    settings = db.query(models.AppSetting).all()
    if not settings:
        return {"timer_enabled": "false", "timer_seconds": "5"}
    return {s.config_key: s.config_value for s in settings}


@app.post("/settings/")
def update_setting(
    key: str,
    value: str,
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin),
):
    setting = db.query(models.AppSetting).filter(models.AppSetting.config_key == key).first()
    if setting:
        setting.config_value = value
    else:
        setting = models.AppSetting(config_key=key, config_value=value)
        db.add(setting)
    db.commit()
    return {"status": "success"}

# --- WAITLIST ---
@app.post("/waitlist/")
def join_waitlist(data: schemas.WaitlistCreate, db: Session = Depends(get_db)):
    new_entry = models.Waitlist(**data.model_dump())
    db.add(new_entry)
    db.commit()
    return {"message": "Success"}
    

# =========================================================
# INSTAGRAM AI AUDIT
# Optimized for concurrent requests / multi-user traffic
# =========================================================

# Legacy single key. Numbered RAPIDAPI_KEY_1..10 are read at call time.
# Tests and older deploys still assign this module global.
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY") or os.getenv("INSTA_LOOTER_KEY")
# Exact client message when every configured RapidAPI key is over quota.
RAPIDAPI_KEYS_EXHAUSTED_DETAIL = "we are getting too much request, please wait"
_RAPIDAPI_EXHAUSTED_UNTIL: Dict[str, float] = {}
_RAPIDAPI_KEY_LOCK = threading.Lock()

# Comma-separated chain (GEMINI_MODELS). Current primary first, then other
# stable flash models that support structured JSON via response_schema:
# https://ai.google.dev/gemini-api/docs/models
# https://ai.google.dev/gemini-api/docs/generate-content/structured-output
DEFAULT_GEMINI_MODELS = "gemini-3.6-flash,gemini-3.8-flash,gemini-3.5-flash-lite"
# One short retry on the same model before switching. Kept small so a Render
# request still finishes while the frontend is waiting.
GEMINI_SAME_MODEL_RETRY_SECONDS = 0.4

# Latest Graph API version documented for Business Discovery (sample calls use v26.0).
# https://developers.facebook.com/docs/graph-api/changelog/
# https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/
DEFAULT_IG_GRAPH_API_VERSION = "v26.0"
_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Public IG User fields plus documented node fields the audit needs
# (name, follows_count, profile_picture_url). Media fields match the
# Business Discovery sample (like_count, comments_count, view_count) and the
# public IG Media fields caption, media_type, timestamp, permalink, shortcode.
# https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/
# https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/
_IG_DISCOVERY_MEDIA_FIELDS = (
    "id,caption,like_count,comments_count,media_type,timestamp,permalink,shortcode,view_count"
)
_IG_GRAPH_RATE_LIMIT_CODES = {4, 17, 32, 341, 613, 80002}
_IG_GRAPH_TOKEN_CODES = {102, 190}
_IG_GRAPH_TOKEN_SUBCODES = {458, 459, 460, 463, 464, 467, 492}

RAPIDAPI_HOST = "instagram-looter2.p.rapidapi.com"
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/profile"

# -------------------------
# Concurrency + cache
# -------------------------
# Do not let a burst of requests overload RapidAPI/Gemini.
# Requests above this limit wait asynchronously instead of blocking the event loop.
AUDIT_MAX_CONCURRENT = int(os.getenv("AUDIT_MAX_CONCURRENT", "50"))
AUDIT_CACHE_TTL = int(os.getenv("AUDIT_CACHE_TTL", "60"))
AUDIT_SEMAPHORE = asyncio.Semaphore(AUDIT_MAX_CONCURRENT)
AUDIT_CACHE_MAX = int(os.getenv("AUDIT_CACHE_MAX", "500"))
AUDIT_CACHE: Dict[str, Any] = {}
AUDIT_INFLIGHT: Dict[str, asyncio.Task] = {}
AUDIT_CACHE_LOCK = asyncio.Lock()


# ---------------------------------------------------------
# Gemini output schema
# ---------------------------------------------------------

class AuditScore(BaseModel):
    score: int = Field(ge=0, le=100)
    reason: str


class GenderStats(BaseModel):
    male: Optional[int] = None
    female: Optional[int] = None
    other: Optional[int] = None
    unknown: Optional[int] = None
    available: bool = False
    source: str = "not_available"


class AuditReport(BaseModel):
    overall_score: int = Field(ge=0, le=100)

    profile: AuditScore
    content: AuditScore
    engagement: AuditScore
    consistency: AuditScore
    growth_potential: AuditScore

    strengths: List[str]
    weaknesses: List[str]
    recommendations: List[str]

    content_strategy: List[str]
    hashtag_strategy: List[str]
    audience_gender: GenderStats

    summary: str


# ---------------------------------------------------------
# Follower gender demographics
# ---------------------------------------------------------

def _number(value: Any) -> Optional[int]:
    """Convert an explicit numeric demographic value to int."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _gender_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def extract_follower_gender(data: dict, followers: int) -> dict:
    """
    Extract ONLY explicitly supplied gender demographics.

    Important: this function never predicts gender from username, name,
    profile photo, captions, or followers. If the RapidAPI response does not
    contain demographic data, the API returns available=false.
    """
    result = {
        "available": False,
        "source": "rapidapi_profile",
        "total_followers": followers,
        "male": None,
        "female": None,
        "other": None,
        "unknown": None,
        "reason": "RapidAPI profile response did not provide follower gender demographics."
    }

    # Different providers use different names for demographic blocks.
    candidates = [
        data.get("follower_demographics"),
        data.get("audience_gender"),
        data.get("audience_gender_age"),
        data.get("gender_demographics"),
        data.get("demographics"),
        data.get("insights", {}).get("follower_demographics") if isinstance(data.get("insights"), dict) else None,
    ]

    block = next((item for item in candidates if item), None)
    if not block:
        return result

    # Normalize a list/dict of rows into (label, value) pairs.
    rows = []
    if isinstance(block, dict):
        if isinstance(block.get("data"), list):
            rows = block["data"]
        elif isinstance(block.get("values"), list):
            rows = block["values"]
        else:
            rows = [{"gender": k, "count": v} for k, v in block.items()]
    elif isinstance(block, list):
        rows = block

    male = female = other = unknown = 0
    found = False

    for row in rows:
        if not isinstance(row, dict):
            continue

        label = (
            row.get("gender")
            or row.get("label")
            or row.get("dimension")
            or row.get("name")
            or row.get("type")
        )
        value = (
            row.get("count")
            if row.get("count") is not None else
            row.get("value")
            if row.get("value") is not None else
            row.get("followers")
            if row.get("followers") is not None else
            row.get("percentage")
        )

        value_num = _number(value)
        if value_num is None:
            continue

        # If the provider gives percentages, convert them to counts.
        if row.get("percentage") is not None and followers > 0 and row.get("count") is None and row.get("value") is None and row.get("followers") is None:
            value_num = int(round(followers * float(row["percentage"]) / 100.0))

        key = _gender_key(label)
        found = True

        if key in {"male", "men", "m", "masculine"}:
            male += value_num
        elif key in {"female", "women", "f", "feminine"}:
            female += value_num
        elif key in {"unknown", "undisclosed", "u", "unspecified"}:
            unknown += value_num
        elif key:
            other += value_num

    if not found:
        return result

    result.update({
        "available": True,
        "male": male,
        "female": female,
        "other": other,
        "unknown": unknown,
        "reason": "Explicit follower demographic data was supplied by the upstream API."
    })
    return result


# ---------------------------------------------------------
# RapidAPI (numbered keys, then the legacy key)
# ---------------------------------------------------------

class _RapidAPIKeyExhausted(Exception):
    """This RapidAPI key is over its quota. Try the next configured key."""


def _rapidapi_cooldown_seconds() -> float:
    raw = os.getenv("RAPIDAPI_KEY_COOLDOWN_SECONDS")
    if raw is None or not str(raw).strip():
        return 60.0
    try:
        value = float(str(raw).strip())
    except ValueError:
        return 60.0
    if value < 0:
        return 60.0
    return value


def _add_rapidapi_key(ordered: List[str], seen: set, value: Optional[str]) -> None:
    if not isinstance(value, str):
        return
    cleaned = value.strip()
    if not cleaned or cleaned in seen:
        return
    seen.add(cleaned)
    ordered.append(cleaned)


def _configured_rapidapi_keys() -> List[str]:
    """RAPIDAPI_KEY_1..10, then RAPIDAPI_KEY, INSTA_LOOTER_KEY, and the module key.

    Duplicates are dropped. The module global stays so tests and older
    deployments that assign RAPIDAPI_KEY directly are still a real key.
    """
    ordered: List[str] = []
    seen: set = set()
    for index in range(1, 11):
        _add_rapidapi_key(ordered, seen, os.getenv(f"RAPIDAPI_KEY_{index}"))
    _add_rapidapi_key(ordered, seen, os.getenv("RAPIDAPI_KEY"))
    _add_rapidapi_key(ordered, seen, os.getenv("INSTA_LOOTER_KEY"))
    _add_rapidapi_key(ordered, seen, RAPIDAPI_KEY)
    return ordered


def _rapidapi_configured() -> bool:
    return bool(_configured_rapidapi_keys())


def _note_rapidapi_key_exhausted(key: str) -> None:
    delay = _rapidapi_cooldown_seconds()
    with _RAPIDAPI_KEY_LOCK:
        _RAPIDAPI_EXHAUSTED_UNTIL[key] = time.monotonic() + delay


def _usable_rapidapi_keys() -> List[str]:
    now = time.monotonic()
    with _RAPIDAPI_KEY_LOCK:
        expired = [
            key for key, until in _RAPIDAPI_EXHAUSTED_UNTIL.items() if until <= now
        ]
        for key in expired:
            _RAPIDAPI_EXHAUSTED_UNTIL.pop(key, None)
        cooled = set(_RAPIDAPI_EXHAUSTED_UNTIL)
    return [key for key in _configured_rapidapi_keys() if key not in cooled]


def clear_rapidapi_key_cooldowns() -> None:
    with _RAPIDAPI_KEY_LOCK:
        _RAPIDAPI_EXHAUSTED_UNTIL.clear()


def _select_rapidapi_keys() -> List[str]:
    if not _configured_rapidapi_keys():
        raise HTTPException(
            status_code=500,
            detail="RAPIDAPI_KEY is not configured",
        )
    usable = _usable_rapidapi_keys()
    if not usable:
        raise HTTPException(
            status_code=429,
            detail=RAPIDAPI_KEYS_EXHAUSTED_DETAIL,
        )
    return usable


def _rapidapi_headers(key: str) -> dict:
    return {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": key,
    }


def _rapidapi_response_body(response: Any) -> str:
    try:
        text = response.text
    except Exception:
        return ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    return text if isinstance(text, str) else ""


def _rapidapi_text_mentions_quota(text: str) -> bool:
    lowered = (text or "").lower()
    if "rate limit" in lowered:
        return True
    return re.search(r"exceeded the\s+.{0,80}?quota", lowered) is not None


def _rapidapi_key_exhausted(status_code: int, body_text: str, payload: Any = None) -> bool:
    """True for quota / rate-limit responses that should try the next key.

    HTTP 429 always rotates. HTTP 403 rotates when the body talks about the
    subscription or quota. A body that says the quota was exceeded, or that a
    rate limit was hit, rotates on any status that is not a real profile.
    """
    if status_code == 429:
        return True
    lowered = (body_text or "").lower()
    if status_code == 403 and any(
        token in lowered for token in ("quota", "subscription", "subscribed")
    ):
        return True
    mentions_quota = _rapidapi_text_mentions_quota(body_text)
    if status_code != 200 and mentions_quota:
        return True
    if status_code == 200 and mentions_quota:
        if not isinstance(payload, dict):
            return True
        unwrapped = unwrap_profile_payload(payload)
        if not _looks_like_profile(payload) and not _looks_like_profile(unwrapped):
            return True
    return False


def _profile_from_rapidapi_response(response: Any) -> dict:
    body_text = _rapidapi_response_body(response)
    status_code = int(response.status_code)
    if status_code != 200 and _rapidapi_key_exhausted(status_code, body_text):
        raise _RapidAPIKeyExhausted()
    if status_code == 404:
        raise HTTPException(status_code=404, detail="Instagram profile not found")
    if status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Instagram profile lookup failed (HTTP {status_code}). "
                "Please try again later."
            ),
        )
    try:
        payload = response.json()
    except ValueError:
        if _rapidapi_key_exhausted(status_code, body_text):
            raise _RapidAPIKeyExhausted()
        raise HTTPException(
            status_code=502,
            detail="RapidAPI returned invalid JSON",
        )
    if _rapidapi_key_exhausted(status_code, body_text, payload):
        raise _RapidAPIKeyExhausted()
    return payload


def _rapidapi_transport_error(exc: BaseException) -> HTTPException:
    # _redact_audit_error is defined later and covers every configured key.
    detail = _redact_audit_error(f"RapidAPI request failed: {exc}")
    return HTTPException(status_code=502, detail=detail)


def get_instagram_profile(username: str) -> dict:
    keys = _select_rapidapi_keys()
    for index, key in enumerate(keys, start=1):
        try:
            response = requests.get(
                RAPIDAPI_URL,
                headers=_rapidapi_headers(key),
                params={"username": username},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise _rapidapi_transport_error(exc)

        print(
            f"📡 RapidAPI /profile: HTTP {response.status_code} "
            f"username=@{username} key={index}/{len(keys)}"
        )
        try:
            return _profile_from_rapidapi_response(response)
        except _RapidAPIKeyExhausted:
            _note_rapidapi_key_exhausted(key)
            print(
                f"⚠️ RapidAPI key {index}/{len(keys)} quota exhausted "
                f"username=@{username}"
            )
            continue
        except HTTPException:
            if response.status_code != 200:
                print(
                    f"❌ RapidAPI /profile HTTP {response.status_code} "
                    f"username=@{username}"
                )
            raise

    raise HTTPException(status_code=429, detail=RAPIDAPI_KEYS_EXHAUSTED_DETAIL)


# ---------------------------------------------------------
# Async RapidAPI profile call
# ---------------------------------------------------------
async def get_instagram_profile_async(username: str) -> dict:
    keys = _select_rapidapi_keys()
    for index, key in enumerate(keys, start=1):
        try:
            response = await http_client.get(
                RAPIDAPI_URL,
                headers=_rapidapi_headers(key),
                params={"username": username},
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )
        except httpx.HTTPStatusError as exc:
            response = exc.response
            if response is None:
                raise _rapidapi_transport_error(exc)
        except httpx.RequestError as exc:
            raise _rapidapi_transport_error(exc)

        print(
            f"📡 RapidAPI /profile: HTTP {response.status_code} "
            f"username=@{username} key={index}/{len(keys)}"
        )
        try:
            return _profile_from_rapidapi_response(response)
        except _RapidAPIKeyExhausted:
            _note_rapidapi_key_exhausted(key)
            print(
                f"⚠️ RapidAPI key {index}/{len(keys)} quota exhausted "
                f"username=@{username}"
            )
            continue
        except HTTPException:
            if response.status_code != 200:
                print(
                    f"❌ RapidAPI /profile HTTP {response.status_code} "
                    f"username=@{username}"
                )
            raise

    raise HTTPException(status_code=429, detail=RAPIDAPI_KEYS_EXHAUSTED_DETAIL)



# ---------------------------------------------------------
# Profile image -> Hostinger permanent storage
# ---------------------------------------------------------
_PROFILE_IMAGE_SUFFIXES = (".cdninstagram.com", ".fbcdn.net", ".instagram.com")


def profile_image_url_allowed(url: str) -> bool:
    """Profile images may only be fetched from Instagram's own HTTPS CDNs."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2000:
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(host == suffix[1:] or host.endswith(suffix) for suffix in _PROFILE_IMAGE_SUFFIXES)


async def save_profile_pic_to_hostinger(img_url: str, username: str):
    """
    Download Instagram CDN profile image and upload it to Hostinger.

    Important:
    - Uses the shared httpx AsyncClient; no blocking requests.post().
    - Sends Instagram-like headers because CDN URLs may reject plain clients.
    - Resizes/re-encodes to WebP before upload.
    - Validates the Hostinger bridge response before returning the permanent URL.
    """
    if not img_url:
        print(f"⚠️ No profile image URL for @{username}")
        return None
    if not profile_image_url_allowed(img_url):
        print(f"⚠️ Skipping profile image with a non-Instagram URL for @{username}")
        return None

    try:
        upload_key = require_upload_handler_key()
    except HTTPException as exc:
        print(f"⚠️ Skipping profile image upload: {exc.detail}")
        return None

    username = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
    file_name = f"{username}_profile.webp"

    try:
        print(f"📸 Downloading Instagram profile image: @{username}")

        instagram_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        }

        img_res = await http_client.get(
            img_url,
            headers=instagram_headers,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
        )

        print(
            f"📥 Instagram image response: "
            f"{img_res.status_code}, "
            f"type={img_res.headers.get('content-type')}, "
            f"bytes={len(img_res.content)}"
        )

        if img_res.status_code != 200:
            print(
                f"❌ Instagram image download failed: "
                f"HTTP {img_res.status_code}"
            )
            return None

        img_data = img_res.content
        if not img_data:
            print("❌ Instagram returned empty image data")
            return None

        try:
            img_data = optimize_image_bytes(
                img_data,
                max_dimension=IMAGE_MAX_DIMENSION,
                quality=IMAGE_WEBP_QUALITY,
                output_format="WEBP",
            )
        except Exception as exc:
            print(f"⚠️ Could not optimize profile image: {exc}")
            return None

        upload_data = {
            "key": upload_key,
            "category": "profiles",
            "filename": file_name,
        }
        upload_files = {
            "file": (
                file_name,
                img_data,
                "image/webp",
            )
        }

        print(
            f"📤 Uploading {file_name} to Hostinger "
            f"({len(img_data)} bytes)"
        )

        upload_response = await http_client.post(
            UPLOAD_HANDLER_URL,
            data=upload_data,
            files=upload_files,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

        response_text = upload_response.text[:2000]

        print(
            f"📡 Hostinger upload response: "
            f"HTTP {upload_response.status_code} | {response_text}"
        )

        if upload_response.status_code != 200:
            print("❌ Hostinger PHP bridge returned non-200")
            return None

        bridge_ok = False
        try:
            bridge_json = upload_response.json()

            if isinstance(bridge_json, dict):
                bridge_status = str(
                    bridge_json.get("status", "")
                ).lower()

                bridge_ok = (
                    bridge_status in {
                        "success",
                        "ok",
                        "uploaded",
                        "true",
                    }
                    or bridge_json.get("success") is True
                    or bridge_json.get("url")
                )
            else:
                bridge_ok = bool(bridge_json)

        except ValueError:
            lowered = response_text.lower()
            bridge_ok = any(
                word in lowered
                for word in (
                    "success",
                    "uploaded",
                    "upload successful",
                    "file uploaded",
                )
            )

        if not bridge_ok:
            print(
                "❌ Hostinger responded 200, but upload was not "
                "confirmed as successful."
            )
            return None

        permanent_url = (
            f"{HOSTINGER_PUBLIC_BASE}/uploads/"
            f"profiles/{file_name}"
        )

        try:
            verify_response = await http_client.get(
                permanent_url,
                headers={"Range": "bytes=0-20"},
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
            )

            if verify_response.status_code >= 400:
                print(
                    f"⚠️ Hostinger upload reported success but URL "
                    f"verification failed: HTTP {verify_response.status_code}"
                )
                return None

        except httpx.RequestError as exc:
            print(f"⚠️ Could not verify Hostinger URL: {str(exc)}")
            return None

        print(f"✅ Permanent profile image: {permanent_url}")
        return permanent_url

    except httpx.TimeoutException:
        print(f"⏱️ Profile image transfer timed out: @{username}")
    except httpx.RequestError as exc:
        print(f"🔥 Profile image HTTP error: {str(exc)}")
    except Exception as exc:
        print(f"🔥 Profile image transfer error: {str(exc)}")

    return None


# ---------------------------------------------------------
# Source-provided follower gender demographics
# ---------------------------------------------------------
def extract_gender_stats(data: dict, total_followers: int) -> dict:
    """
    Use only explicit aggregate demographic data supplied by the upstream API.
    Never infer/guess a person's gender from name, bio, photo, or username.
    """
    candidates = [
        data.get("follower_demographics"),
        data.get("audience_gender"),
        data.get("gender_distribution"),
        data.get("audience", {}).get("gender")
            if isinstance(data.get("audience"), dict) else None,
    ]

    raw = next((x for x in candidates if isinstance(x, dict)), None)

    if not raw:
        return {
            "male": None,
            "female": None,
            "other": None,
            "unknown": None,
            "available": False,
            "source": "not_available",
            "total_followers": total_followers,
        }

    def first_number(*keys):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return None

    male = first_number("male", "Male", "MALE")
    female = first_number("female", "Female", "FEMALE")
    other = first_number("other", "Other", "OTHER")
    unknown = first_number("unknown", "Unknown", "UNKNOWN")

    return {
        "male": male,
        "female": female,
        "other": other,
        "unknown": unknown,
        "available": any(v is not None for v in (male, female, other, unknown)),
        "source": "rapidapi_profile",
        "total_followers": total_followers,
    }

# ---------------------------------------------------------
# Extract profile metrics
# ---------------------------------------------------------

def _as_bool(value: Any) -> bool:
    """Coerce API booleans. The string "false" must not count as private."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _first_int(data: dict, paths: Tuple[Tuple[str, ...], ...], default: int = 0) -> int:
    """First present numeric field. A null parent does not hide later fallbacks."""
    for path in paths:
        current: Any = data
        found = True
        for part in path:
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if not found or current is None or isinstance(current, bool):
            continue
        try:
            return int(current)
        except (TypeError, ValueError):
            continue
    return default


def _profile_not_found(data: dict) -> bool:
    if not isinstance(data, dict):
        return True
    status = data.get("status")
    if status is False or status == 0:
        return True
    if isinstance(status, str) and status.strip().lower() in {"false", "error", "not_found", "not found"}:
        return True
    for key in ("message", "error", "detail"):
        value = data.get(key)
        if isinstance(value, str) and any(
            phrase in value.lower() for phrase in ("not found", "does not exist", "user not found")
        ):
            return True
    return False


def _looks_like_profile(data: dict) -> bool:
    return any(
        key in data
        for key in (
            "username",
            "edge_followed_by",
            "follower_count",
            "followers_count",
            "biography",
        )
    )


def unwrap_profile_payload(data: dict) -> dict:
    """Accept the web-profile shape and the wrapped shapes some providers return."""
    if not isinstance(data, dict):
        return {}
    if _looks_like_profile(data):
        return data
    graphql = data.get("graphql")
    if isinstance(graphql, dict) and isinstance(graphql.get("user"), dict):
        if _looks_like_profile(graphql["user"]):
            return graphql["user"]
    for key in ("user", "data", "profile"):
        inner = data.get(key)
        if isinstance(inner, dict) and _looks_like_profile(inner):
            return inner
    return data


def extract_profile_metrics(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    full_name = data.get("full_name") or data.get("fullName") or data.get("name")
    biography = data.get("biography") if isinstance(data.get("biography"), str) else data.get("bio")
    pic = data.get("profile_pic_url_hd") or data.get("profile_pic_url") or data.get("profile_picture_url")
    return {
        "username": data.get("username"),
        "full_name": full_name if isinstance(full_name, str) else None,
        "biography": biography if isinstance(biography, str) else None,
        "followers": _first_int(
            data,
            (
                ("edge_followed_by", "count"),
                ("follower_count",),
                ("followers_count",),
                ("followers",),
            ),
        ),
        "following": _first_int(
            data,
            (
                ("edge_follow", "count"),
                ("following_count",),
                ("follows_count",),
                ("following",),
            ),
        ),
        "total_posts": _first_int(
            data,
            (
                ("edge_owner_to_timeline_media", "count"),
                ("media_count",),
                ("posts_count",),
                ("total_posts",),
            ),
        ),
        "is_private": _as_bool(data.get("is_private")),
        "is_verified": _as_bool(data.get("is_verified")),
        "is_professional_account": _as_bool(data.get("is_professional_account")),
        "is_business_account": _as_bool(data.get("is_business_account")),
        "profile_pic_url": pic if isinstance(pic, str) else None,
    }


# ---------------------------------------------------------
# Extract last 10 posts
# ---------------------------------------------------------

def _caption_text(post: dict) -> str:
    container = post.get("edge_media_to_caption")
    if isinstance(container, dict):
        edges = container.get("edges")
        if isinstance(edges, list) and edges and isinstance(edges[0], dict):
            node = edges[0].get("node")
            if isinstance(node, dict) and isinstance(node.get("text"), str):
                return node["text"]
    caption = post.get("caption")
    if isinstance(caption, str):
        return caption
    if isinstance(caption, dict) and isinstance(caption.get("text"), str):
        return caption["text"]
    return ""


def _named_count(post: dict, edge_key: str, flat_keys: Tuple[str, ...]) -> int:
    container = post.get(edge_key)
    if isinstance(container, dict):
        return _as_int(container.get("count"))
    for key in flat_keys:
        if key in post and post.get(key) is not None:
            return _as_int(post.get(key))
    return 0


def _is_video_post(post: dict, typename: Any) -> bool:
    flag = post.get("is_video")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return _as_bool(flag)
    return str(typename or "").lower() in {"video", "graphvideo", "reel", "clips"}


def _post_from_item(post: dict) -> dict:
    typename = post.get("__typename") or post.get("media_type") or post.get("type")
    views = 0
    for key in ("video_view_count", "view_count", "video_views"):
        if key in post and post.get(key) is not None:
            views = _as_int(post.get(key))
            break
    return {
        "id": post.get("id"),
        "shortcode": post.get("shortcode"),
        "type": typename,
        "caption": _caption_text(post),
        "likes": _named_count(post, "edge_liked_by", ("like_count", "likes")),
        "comments": _named_count(post, "edge_media_to_comment", ("comments_count", "comment_count", "comments")),
        "video_views": views,
        "timestamp": post.get("taken_at_timestamp") or post.get("timestamp"),
        "is_video": _is_video_post(post, typename),
    }


def extract_recent_posts(data: dict, limit: int = 10) -> list:
    if not isinstance(data, dict):
        return []
    posts = []
    media = data.get("edge_owner_to_timeline_media")
    edges = media.get("edges") if isinstance(media, dict) else None
    if isinstance(edges, list):
        for edge in edges:
            if len(posts) >= limit:
                break
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if isinstance(node, dict):
                posts.append(_post_from_item(node))
    if posts:
        return posts
    for key in ("posts", "items", "medias"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if len(posts) >= limit:
                break
            if isinstance(row, dict):
                posts.append(_post_from_item(row))
        if posts:
            return posts
    return posts


# ---------------------------------------------------------
# Engagement calculation
# ---------------------------------------------------------

def calculate_engagement(followers: int, posts: list) -> dict:
    if not posts:
        return {
            "average_likes": 0,
            "average_comments": 0,
            "average_video_views": 0,
            "average_engagement_rate": 0,
            "total_engagement": 0,
        }

    total_likes = sum(post.get("likes", 0) or 0 for post in posts)
    total_comments = sum(post.get("comments", 0) or 0 for post in posts)
    total_views = sum(post.get("video_views", 0) or 0 for post in posts)
    post_count = len(posts)

    average_likes = total_likes / post_count
    average_comments = total_comments / post_count
    average_views = total_views / post_count

    if followers > 0:
        engagement_rates = [
            ((post.get("likes", 0) or 0) + (post.get("comments", 0) or 0)) / followers * 100
            for post in posts
        ]
        average_engagement_rate = sum(engagement_rates) / len(engagement_rates)
    else:
        average_engagement_rate = 0

    return {
        "average_likes": round(average_likes, 2),
        "average_comments": round(average_comments, 2),
        "average_video_views": round(average_views, 2),
        "average_engagement_rate": round(average_engagement_rate, 4),
        "total_engagement": total_likes + total_comments,
    }


# ---------------------------------------------------------
# Gemini audit
# ---------------------------------------------------------

_FATAL_HTTP_CODES = {400, 401, 402, 403, 409, 422}
_TRANSIENT_HTTP_CODES = {404, 408, 429, 500, 502, 503, 504}
_FATAL_STATUSES = {
    "INVALID_ARGUMENT",
    "FAILED_PRECONDITION",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "OUT_OF_RANGE",
}
_TRANSIENT_STATUSES = {
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
    "ABORTED",
    "NOT_FOUND",
}
_HTTP_CODE_IN_TEXT = re.compile(
    r"\b(400|401|402|403|404|408|409|422|429|500|502|503|504)\b"
)
_STATUS_IN_TEXT = re.compile(
    r"\b(UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|INTERNAL|"
    r"UNAUTHENTICATED|PERMISSION_DENIED|INVALID_ARGUMENT|FAILED_PRECONDITION|"
    r"NOT_FOUND|ABORTED|OUT_OF_RANGE)\b"
)
_DEMAND_PHRASES = (
    "high demand",
    "overloaded",
    "resource exhausted",
    "try again later",
    "temporarily unavailable",
)


def gemini_model_chain() -> List[str]:
    """Models to try, in order. GEMINI_MODELS overrides the defaults."""
    raw = os.getenv("GEMINI_MODELS")
    if raw is None or not raw.strip():
        raw = DEFAULT_GEMINI_MODELS

    models: List[str] = []
    seen = set()
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        models.append(name)

    if models:
        return models

    return [part.strip() for part in DEFAULT_GEMINI_MODELS.split(",") if part.strip()]


def _iter_exception_chain(exc: BaseException):
    seen = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.append(current.__cause__)
        pending.append(current.__context__)


def _exception_http_code(exc: BaseException) -> Optional[int]:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _exception_status_name(exc: BaseException) -> Optional[str]:
    value = getattr(exc, "status", None)
    if isinstance(value, str) and value.replace("_", "").isalpha():
        return value.upper()
    return None


def _is_timeout_exception(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    return isinstance(exc, TimeoutError)


def _error_signals(exc: BaseException) -> Tuple[List[int], List[str], bool, bool]:
    codes: List[int] = []
    statuses: List[str] = []
    timed_out = False
    overloaded = False

    for current in _iter_exception_chain(exc):
        code = _exception_http_code(current)
        status = _exception_status_name(current)
        if code is not None:
            codes.append(code)
        if status:
            statuses.append(status)
        if _is_timeout_exception(current):
            timed_out = True

        text = str(current)
        if code is None:
            match = _HTTP_CODE_IN_TEXT.search(text)
            if match:
                codes.append(int(match.group(1)))
        if not status:
            match = _STATUS_IN_TEXT.search(text)
            if match:
                statuses.append(match.group(1))

        lowered = text.lower()
        if any(phrase in lowered for phrase in _DEMAND_PHRASES):
            overloaded = True
        if "timed out" in lowered or "timeout" in lowered or "deadline exceeded" in lowered:
            timed_out = True

    return codes, statuses, timed_out, overloaded


def is_transient_gemini_error(exc: BaseException) -> bool:
    """True when another model or a short retry might still produce the audit."""
    codes, statuses, timed_out, overloaded = _error_signals(exc)
    if any(code in _FATAL_HTTP_CODES for code in codes):
        return False
    if any(status in _FATAL_STATUSES for status in statuses):
        return False
    if any(code in _TRANSIENT_HTTP_CODES for code in codes):
        return True
    if any(status in _TRANSIENT_STATUSES for status in statuses):
        return True
    return timed_out or overloaded


def _retry_same_model(exc: BaseException) -> bool:
    """Retry overload/timeouts once. A missing model will not recover in place."""
    if not is_transient_gemini_error(exc):
        return False
    codes, statuses, _, _ = _error_signals(exc)
    if 404 in codes or "NOT_FOUND" in statuses:
        return False
    return True


def _safe_error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in (os.getenv("GEMINI_API_KEY"), GEMINI_API_KEY):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = " ".join(text.split())
    if len(text) > 300:
        text = text[:300] + "..."
    return text


def _fatal_gemini_detail(model: str, exc: BaseException) -> str:
    codes, statuses, _, _ = _error_signals(exc)
    code = codes[0] if codes else None
    if code in (401, 403) or any(status in {"UNAUTHENTICATED", "PERMISSION_DENIED"} for status in statuses):
        suffix = f" ({code})" if code else ""
        return (
            "Gemini audit generation failed: API key was rejected"
            f"{suffix}. Check GEMINI_API_KEY. Not falling back to another model."
        )
    if code in (400, 422) or "INVALID_ARGUMENT" in statuses:
        suffix = f" ({code})" if code else ""
        return (
            "Gemini audit generation failed: the request was rejected as invalid "
            f"by {model}{suffix}. Not falling back to another model."
        )
    return (
        "Gemini audit generation failed: non-retryable error from "
        f"{model}: {_safe_error_text(exc)}. Not falling back to another model."
    )


def generate_ai_audit(
    profile: dict, posts: list, engagement: dict, gender: dict
) -> Tuple[AuditReport, str]:
    audit_data = {
        "profile": profile,
        "recent_posts": posts,
        "engagement": engagement,
        "follower_gender": gender,
    }

    prompt = f"""
You are an expert Instagram growth and content strategist.

Analyze ONLY the Instagram data supplied below.

STRICT RULES:
- Use ONLY supplied data.
- Never invent metrics, followers, demographics, or facts.
- Never infer gender from names, usernames, profile photos, captions, or any other proxy.
- If follower_gender.available is false, explicitly say gender demographics are unavailable.
- Do not invent male/female/other counts.
- Do not mention unrelated people, brands, or names.
- Do not add random words to recommendations.
- Every recommendation must be grounded in the supplied account data.
- Return ONLY valid JSON matching the schema.

Instagram data:
{json.dumps(audit_data, ensure_ascii=False)}

Evaluate:
1. Profile optimization
2. Content quality
3. Engagement
4. Posting consistency
5. Growth potential
6. Content strategy
7. Hashtag strategy
8. Strengths
9. Weaknesses
10. Actionable recommendations
"""

    models = gemini_model_chain()
    # A client is created per request/thread, avoiding shared mutable client state.
    client = genai.Client(api_key=GEMINI_API_KEY)
    failures: List[str] = []

    for index, model in enumerate(models):
        attempt = 1
        while attempt <= 2:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": AuditReport,
                    },
                )

                if response.parsed:
                    report = response.parsed
                else:
                    report = AuditReport.model_validate_json(response.text)

                print(f"✅ Gemini audit produced by {model}")
                return report, model

            except HTTPException:
                raise
            except Exception as exc:
                if not is_transient_gemini_error(exc):
                    raise HTTPException(
                        status_code=502,
                        detail=_fatal_gemini_detail(model, exc),
                    )

                summary = _safe_error_text(exc)
                failures.append(f"{model}: {summary}")
                if attempt == 1 and _retry_same_model(exc):
                    print(f"⚠️ Gemini model {model} unavailable ({summary}); retrying once")
                    time.sleep(GEMINI_SAME_MODEL_RETRY_SECONDS)
                    attempt += 1
                    continue

                next_model = models[index + 1] if index + 1 < len(models) else None
                if next_model:
                    print(
                        f"⚠️ Gemini model {model} unavailable ({summary}); trying {next_model}"
                    )
                break

    detail = (
        "Gemini audit generation failed: all models were unavailable "
        f"({', '.join(models)}). " + " | ".join(failures)
    )
    if len(detail) > 1500:
        detail = detail[:1500] + "..."
    raise HTTPException(status_code=502, detail=detail)


# ---------------------------------------------------------
# One complete audit job
# ---------------------------------------------------------

def _redact_audit_error(text: str) -> str:
    secrets: List[Optional[str]] = [
        RAPIDAPI_KEY,
        GEMINI_API_KEY,
        os.getenv("RAPIDAPI_KEY"),
        os.getenv("INSTA_LOOTER_KEY"),
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GROQ_API_KEY"),
        os.getenv("IG_GRAPH_ACCESS_TOKEN"),
    ]
    for index in range(1, 11):
        secrets.append(os.getenv(f"RAPIDAPI_KEY_{index}"))
    secrets.extend(_configured_rapidapi_keys())
    with _RAPIDAPI_KEY_LOCK:
        secrets.extend(_RAPIDAPI_EXHAUSTED_UNTIL.keys())
    seen = set()
    ordered: List[str] = []
    for secret in secrets:
        if not isinstance(secret, str):
            continue
        cleaned = secret.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    for secret in sorted(ordered, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = " ".join(text.split())
    if len(text) > 300:
        text = text[:300] + "..."
    return text


async def _reserve_audit(feature_id: str) -> None:
    """Stop before any paid upstream call once the hard budget is spent."""
    await asyncio.to_thread(
        ai_gateway.begin_generation,
        feature_id,
        ai_gateway.current_client_key(),
    )


async def run_audit_job(username: str, *, reserve: bool = True) -> dict:
    """Complete non-blocking audit job for one username."""
    if not _IG_USERNAME_RE.fullmatch(username or ""):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")
    if reserve:
        await _reserve_audit("insta-audit")
    raw_profile = await get_instagram_profile_async(username)

    if not isinstance(raw_profile, dict) or _profile_not_found(raw_profile):
        raise HTTPException(
            status_code=404,
            detail="Instagram profile not found",
        )

    profile_data = unwrap_profile_payload(raw_profile)
    profile = extract_profile_metrics(profile_data)

    if profile.get("is_private"):
        return {
            "status": "private",
            "username": username,
            "profile": profile,
            "message": "Account is private. Make it public first!",
        }

    if not profile.get("username"):
        raise HTTPException(status_code=404, detail="Instagram profile not found")

    # Persist profile image on Hostinger.
    original_url = profile.get("profile_pic_url")
    permanent_url = await save_profile_pic_to_hostinger(
        original_url,
        username,
    )

    if permanent_url:
        profile["profile_pic_url"] = permanent_url
    else:
        print(
            f"⚠️ Using original Instagram URL for @{username}"
        )

    recent_posts = extract_recent_posts(
        profile_data,
        limit=10,
    )
    engagement = calculate_engagement(
        profile["followers"],
        recent_posts,
    )
    gender = extract_follower_gender(
        profile_data,
        profile["followers"],
    )

    # Current Gemini function in this file is synchronous.
    audit, audit_model = await asyncio.to_thread(
        generate_ai_audit,
        profile=profile,
        posts=recent_posts,
        engagement=engagement,
        gender=gender,
    )

    return {
        "status": "success",
        "username": username,
        "profile": profile,
        "recent_posts": recent_posts,
        "engagement": engagement,
        "follower_gender": gender,
        "ai_audit": audit.model_dump(),
        "ai_audit_model": audit_model,
    }


# ---------------------------------------------------------
# Concurrent-safe cache / request de-duplication
# ---------------------------------------------------------

async def get_cached_or_run_audit(
    username: str,
    runner=None,
    cache_key: Optional[str] = None,
) -> dict:
    """Share one cache, in-flight map, and semaphore across audit sources.

    The RapidAPI route keeps using the bare username as its cache key.
    Other sources pass their own cache_key so the two payloads never mix,
    while still sharing AUDIT_SEMAPHORE.
    """
    key = cache_key or username
    if runner is None:
        async def runner(name: str) -> dict:
            return await run_audit_job(name)

    now = time.monotonic()

    async with AUDIT_CACHE_LOCK:
        cached = AUDIT_CACHE.get(key)
        if cached and now - cached["created_at"] < AUDIT_CACHE_TTL:
            return cached["data"]

        # If the same username is already being processed, all callers await
        # that task rather than starting duplicate profile + Gemini calls.
        existing_task = AUDIT_INFLIGHT.get(key)
        if existing_task:
            task = existing_task
        else:
            async def worker():
                async with AUDIT_SEMAPHORE:
                    return await runner(username)

            task = asyncio.create_task(worker())
            AUDIT_INFLIGHT[key] = task

    try:
        result = await task

        async with AUDIT_CACHE_LOCK:
            AUDIT_CACHE[key] = {
                "created_at": time.monotonic(),
                "data": result,
            }

            # Keep memory bounded when many different usernames are audited.
            if len(AUDIT_CACHE) > AUDIT_CACHE_MAX:
                oldest_username = min(
                    AUDIT_CACHE,
                    key=lambda key: AUDIT_CACHE[key]["created_at"],
                )
                AUDIT_CACHE.pop(oldest_username, None)

        return result

    finally:
        async with AUDIT_CACHE_LOCK:
            if AUDIT_INFLIGHT.get(key) is task:
                AUDIT_INFLIGHT.pop(key, None)


# ---------------------------------------------------------
# Instagram Graph API Business Discovery (no RapidAPI)
# ---------------------------------------------------------

def ig_graph_api_version() -> str:
    raw = (os.getenv("IG_GRAPH_API_VERSION") or DEFAULT_IG_GRAPH_API_VERSION).strip()
    if not raw.lower().startswith("v"):
        raw = f"v{raw}"
    else:
        raw = "v" + raw[1:]
    if not re.fullmatch(r"v\d+\.\d+", raw):
        raise HTTPException(
            status_code=500,
            detail="IG_GRAPH_API_VERSION must look like v26.0",
        )
    return raw


def ig_graph_credentials() -> Tuple[str, str, str]:
    token = (os.getenv("IG_GRAPH_ACCESS_TOKEN") or "").strip()
    account_id = (os.getenv("IG_BUSINESS_ACCOUNT_ID") or "").strip()
    if not token or not account_id:
        raise HTTPException(
            status_code=500,
            detail=(
                "Instagram Graph API is not configured. "
                "Set IG_GRAPH_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID."
            ),
        )
    if not re.fullmatch(r"\d+", account_id):
        raise HTTPException(
            status_code=500,
            detail=(
                "IG_BUSINESS_ACCOUNT_ID must be the numeric Instagram "
                "professional account id from the Facebook Page."
            ),
        )
    return token, account_id, ig_graph_api_version()


def ig_graph_configured() -> bool:
    """True when a Business Discovery call can be attempted. Does not raise."""
    token = (os.getenv("IG_GRAPH_ACCESS_TOKEN") or "").strip()
    account_id = (os.getenv("IG_BUSINESS_ACCOUNT_ID") or "").strip()
    if not token or not re.fullmatch(r"\d+", account_id):
        return False
    try:
        ig_graph_api_version()
    except HTTPException:
        return False
    return True


def business_discovery_fields(username: str, include_extended: bool = True) -> str:
    """Field expansion for one public Business or Creator account.

    The username is interpolated into the Graph field string, so it has to
    match Instagram's username alphabet before it is sent.

    Fields marked Public on the IG User reference are always requested.
    name, follows_count, and profile_picture_url are documented IG User fields
    the audit uses; if Graph rejects them as non-public, the caller retries
    without them.
    """
    if not _IG_USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="Instagram username is invalid",
        )
    profile_fields = "id,username,biography,website,followers_count,media_count"
    if include_extended:
        profile_fields = (
            "id,username,name,biography,website,followers_count,follows_count,"
            "media_count,profile_picture_url"
        )
    return (
        f"business_discovery.username({username})"
        f"{{{profile_fields},media.limit(10){{{_IG_DISCOVERY_MEDIA_FIELDS}}}}}"
    )


def _graph_rejected_extended_field(body: Any) -> bool:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("code") != 100:
        return False
    message = str(error.get("message") or "").lower()
    return "nonexisting field" in message or "tried accessing" in message


def _redact_graph_secret(text: str) -> str:
    token = (os.getenv("IG_GRAPH_ACCESS_TOKEN") or "").strip()
    if token:
        text = text.replace(token, "[redacted]")
    return re.sub(r"access_token=[^&\s]+", "access_token=[redacted]", text)


def _graph_error_exception(status_code: int, body: Any, username: str) -> HTTPException:
    error = body.get("error") if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    message = " ".join(
        str(part)
        for part in (
            error.get("message"),
            error.get("error_user_title"),
            error.get("error_user_msg"),
        )
        if part
    )
    lowered = message.lower()
    safe = _redact_graph_secret(message)[:500]

    if status_code == 429 or code in _IG_GRAPH_RATE_LIMIT_CODES:
        return HTTPException(
            status_code=429,
            detail="Instagram Graph API rate limit reached. Wait and try again.",
        )

    token_rejected = (
        code in _IG_GRAPH_TOKEN_CODES
        or subcode in _IG_GRAPH_TOKEN_SUBCODES
        or (
            "access token" in lowered
            and any(word in lowered for word in ("expired", "invalid", "session", "revoked"))
        )
    )
    if token_rejected:
        return HTTPException(
            status_code=401,
            detail=(
                "Instagram Graph access token is invalid or expired. "
                "Generate a new long-lived token and update IG_GRAPH_ACCESS_TOKEN."
            ),
        )

    # Business Discovery uses code 110 / subcode 2207013 ("Cannot find User")
    # when the username does not exist or is not a Business/Creator account.
    target_missing = (
        code == 110
        or subcode == 2207013
        or "cannot find user" in lowered
        or "cannot be found" in lowered
    )
    if target_missing:
        return HTTPException(
            status_code=404,
            detail=(
                f"Instagram account @{username} was not found, or Business Discovery "
                "cannot read it. The account must be a public Business or Creator account. "
                "Personal accounts are not available."
            ),
        )

    if code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        return HTTPException(
            status_code=403,
            detail=(
                "Instagram Graph token is missing permissions. It needs "
                "instagram_basic, instagram_manage_insights, and pages_read_engagement."
            ),
        )

    return HTTPException(
        status_code=502,
        detail=(
            "Instagram Graph API request failed "
            f"(HTTP {status_code}, code {code}): {safe or 'unknown error'}"
        ),
    )


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _shortcode_from_permalink(permalink: Optional[str]) -> Optional[str]:
    if not permalink:
        return None
    match = re.search(r"/(?:p|reel|tv)/([^/?#]+)", permalink)
    return match.group(1) if match else None


def map_graph_profile(discovery: dict) -> dict:
    return {
        "username": discovery.get("username"),
        "full_name": discovery.get("name"),
        "biography": discovery.get("biography"),
        "website": discovery.get("website"),
        "followers": _as_int(discovery.get("followers_count")),
        "following": _as_int(discovery.get("follows_count")),
        "total_posts": _as_int(discovery.get("media_count")),
        "is_private": False,
        "is_verified": None,
        "is_professional_account": True,
        "is_business_account": None,
        "profile_pic_url": discovery.get("profile_picture_url"),
    }


def map_graph_posts(discovery: dict, limit: int = 10) -> list:
    media = discovery.get("media") if isinstance(discovery.get("media"), dict) else {}
    rows = media.get("data") if isinstance(media.get("data"), list) else []
    posts = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        media_type = item.get("media_type")
        permalink = item.get("permalink")
        # Graph omits like_count when the owner hides like counts.
        posts.append({
            "id": item.get("id"),
            "shortcode": item.get("shortcode") or _shortcode_from_permalink(permalink),
            "type": media_type,
            "caption": item.get("caption") or "",
            "likes": _as_int(item.get("like_count")),
            "comments": _as_int(item.get("comments_count")),
            "video_views": _as_int(item.get("view_count")),
            "timestamp": item.get("timestamp"),
            "permalink": permalink,
            "is_video": media_type == "VIDEO",
        })
    return posts


def unavailable_follower_gender(followers: int) -> dict:
    return {
        "available": False,
        "source": "not_available",
        "total_followers": followers,
        "male": None,
        "female": None,
        "other": None,
        "unknown": None,
        "reason": (
            "Instagram Graph Business Discovery does not provide follower gender demographics."
        ),
    }


async def _graph_get(url: str, token: str, fields: str):
    return await http_client.get(
        url,
        params={"fields": fields, "access_token": token},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )


async def fetch_business_discovery(username: str) -> dict:
    token, account_id, version = ig_graph_credentials()
    url = f"https://graph.facebook.com/{version}/{account_id}"
    # Ask for the extended profile fields first. If this Graph version rejects
    # one of them, repeat with only the fields marked Public.
    field_sets = (
        business_discovery_fields(username, include_extended=True),
        business_discovery_fields(username, include_extended=False),
    )

    for index, fields in enumerate(field_sets):
        try:
            response = await _graph_get(url, token, fields)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Instagram Graph API request failed: {_redact_graph_secret(str(exc))}",
            )

        print(
            f"📡 Graph business_discovery: HTTP {response.status_code} username=@{username}"
        )

        try:
            body = response.json()
        except ValueError:
            body = {}

        rejected_field = _graph_rejected_extended_field(body)
        if rejected_field and index == 0:
            print(
                f"⚠️ Graph rejected an extended profile field for @{username}; "
                "retrying with public fields only"
            )
            continue

        if response.status_code != 200 or (isinstance(body, dict) and body.get("error")):
            raise _graph_error_exception(response.status_code, body, username)

        discovery = body.get("business_discovery") if isinstance(body, dict) else None
        if not isinstance(discovery, dict):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Instagram account @{username} was not found, or Business Discovery "
                    "cannot read it. The account must be a public Business or Creator account. "
                    "Personal accounts are not available."
                ),
            )
        return discovery

    raise HTTPException(
        status_code=502,
        detail="Instagram Graph API rejected the Business Discovery field list.",
    )


async def run_graph_audit_job(
    username: str,
    *,
    reserve: bool = True,
    discovery: Optional[dict] = None,
) -> dict:
    """Audit one username using Graph Business Discovery and the shared Gemini chain."""
    if not _IG_USERNAME_RE.fullmatch(username or ""):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")
    if reserve:
        await _reserve_audit("insta-audit-v2")
    if discovery is None:
        discovery = await fetch_business_discovery(username)
    profile = map_graph_profile(discovery)

    original_url = profile.get("profile_pic_url")
    permanent_url = await save_profile_pic_to_hostinger(original_url, username)
    if permanent_url:
        profile["profile_pic_url"] = permanent_url
    else:
        print(f"⚠️ Using original profile image URL for @{username}")

    recent_posts = map_graph_posts(discovery, limit=10)
    engagement = calculate_engagement(profile["followers"], recent_posts)
    gender = unavailable_follower_gender(profile["followers"])

    audit, audit_model = await asyncio.to_thread(
        generate_ai_audit,
        profile=profile,
        posts=recent_posts,
        engagement=engagement,
        gender=gender,
    )

    return {
        "status": "success",
        "username": username,
        "profile": profile,
        "recent_posts": recent_posts,
        "engagement": engagement,
        "follower_gender": gender,
        "ai_audit": audit.model_dump(),
        "ai_audit_model": audit_model,
        "source": "instagram_graph",
    }


def _graph_discovery_usable(discovery: Any) -> bool:
    """A Business Discovery payload we can score without calling RapidAPI."""
    if not isinstance(discovery, dict):
        return False
    username = discovery.get("username")
    return isinstance(username, str) and bool(username.strip())


def _log_graph_fallback(username: str, detail: Any) -> None:
    safe = _redact_audit_error(_redact_graph_secret(str(detail or "")))
    print(f"⚠️ Graph audit unavailable for @{username}: {safe}; trying RapidAPI")


async def run_auto_audit_job(username: str) -> dict:
    """Try Graph first. Fall back to RapidAPI without a second budget reserve.

    The reserve happens once, before either upstream call, under
    insta-audit-auto. The inner jobs skip their own reserve. Gemini runs only
    inside the source that actually returned a profile, so a Graph miss does
    not call Gemini and then call it again on the RapidAPI path.
    """
    if not _IG_USERNAME_RE.fullmatch(username or ""):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")
    await _reserve_audit("insta-audit-auto")

    if ig_graph_configured():
        discovery = None
        try:
            discovery = await fetch_business_discovery(username)
        except HTTPException as exc:
            _log_graph_fallback(username, exc.detail)
        except Exception as exc:
            _log_graph_fallback(username, f"{type(exc).__name__}: {exc}")
        if _graph_discovery_usable(discovery):
            result = await run_graph_audit_job(
                username,
                reserve=False,
                discovery=discovery,
            )
            return {**result, "source": "graph"}

    result = await run_audit_job(username, reserve=False)
    return {**result, "source": "rapidapi"}


# ---------------------------------------------------------
# FINAL API
# ---------------------------------------------------------

@app.get("/ai-magic/insta-audit/{username}")
async def instagram_audit(username: str, request: Request):
    username = username.strip().lstrip("@").lower()

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Instagram username is required",
        )

    if not _IG_USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")

    if not _rapidapi_configured():
        raise HTTPException(
            status_code=500,
            detail="RAPIDAPI_KEY is not configured",
        )

    if not (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY):
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not configured",
        )

    caller = client_ip(request)

    async def runner(name: str) -> dict:
        # Set inside the worker task so the budget sees this caller even when
        # the lookup is scheduled on a new asyncio task.
        token = ai_gateway.set_client_key(caller)
        try:
            return await run_audit_job(name)
        finally:
            ai_gateway.reset_client_key(token)

    try:
        print(f"🚀 Audit request received: @{username}")
        result = await get_cached_or_run_audit(username, runner=runner)
        print(f"✅ Audit completed: @{username}")
        return result

    except HTTPException:
        raise

    except Exception as exc:
        safe = _redact_audit_error(f"{type(exc).__name__}: {exc}")
        print(f"🔥 AUDIT ERROR @{username}: {safe}")
        raise HTTPException(
            status_code=500,
            detail=f"Instagram audit failed: {safe}",
        )


@app.options("/ai-magic/insta-audit-v2/{username}")
async def insta_audit_v2_options(username: str):
    return {"status": "ok"}


@app.get("/ai-magic/insta-audit-v2/{username}")
async def instagram_audit_v2(username: str, request: Request):
    """Public Business/Creator audit via Instagram Graph Business Discovery."""
    username = username.strip().lstrip("@").lower()

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Instagram username is required",
        )

    if not _IG_USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")

    # Validate configuration before touching the cache so a missing token
    # fails the same way on every request.
    ig_graph_credentials()

    if not (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY):
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not configured",
        )

    caller = client_ip(request)

    async def runner(name: str) -> dict:
        token = ai_gateway.set_client_key(caller)
        try:
            return await run_graph_audit_job(name)
        finally:
            ai_gateway.reset_client_key(token)

    try:
        print(f"🚀 Graph audit request received: @{username}")
        result = await get_cached_or_run_audit(
            username,
            runner=runner,
            cache_key=f"graph:{username}",
        )
        print(f"✅ Graph audit completed: @{username}")
        return result

    except HTTPException:
        raise

    except Exception as exc:
        safe = _redact_audit_error(_redact_graph_secret(f"{type(exc).__name__}: {exc}"))
        print(f"🔥 GRAPH AUDIT ERROR @{username}: {safe}")
        raise HTTPException(
            status_code=500,
            detail=f"Instagram Graph audit failed: {safe}",
        )


@app.options("/ai-magic/insta-audit-auto/{username}")
async def insta_audit_auto_options(username: str):
    return {"status": "ok"}


@app.get("/ai-magic/insta-audit-auto/{username}")
async def instagram_audit_auto(username: str, request: Request):
    """Graph Business Discovery first, then RapidAPI when Graph cannot score it."""
    username = username.strip().lstrip("@").lower()

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Instagram username is required",
        )

    if not _IG_USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=400, detail="Instagram username is invalid")

    # A configured Graph token can satisfy the request alone. When Graph is
    # missing, RapidAPI has to be configured or there is nothing to call.
    if not ig_graph_configured() and not _rapidapi_configured():
        raise HTTPException(
            status_code=500,
            detail="RAPIDAPI_KEY is not configured",
        )

    if not (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY):
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not configured",
        )

    caller = client_ip(request)

    async def runner(name: str) -> dict:
        token = ai_gateway.set_client_key(caller)
        try:
            return await run_auto_audit_job(name)
        finally:
            ai_gateway.reset_client_key(token)

    try:
        print(f"🚀 Auto audit request received: @{username}")
        result = await get_cached_or_run_audit(
            username,
            runner=runner,
            cache_key=f"auto:{username}",
        )
        print(f"✅ Auto audit completed: @{username} source={result.get('source')}")
        return result

    except HTTPException:
        raise

    except Exception as exc:
        safe = _redact_audit_error(_redact_graph_secret(f"{type(exc).__name__}: {exc}"))
        print(f"🔥 AUTO AUDIT ERROR @{username}: {safe}")
        raise HTTPException(
            status_code=500,
            detail=f"Instagram audit failed: {safe}",
        )


@app.post("/ai-magic/viral-insta-songs")
async def viral_insta_songs(body: ai_gateway.ViralSongsRequest, request: Request):
    """Free-tier audio ideas for one niche. Cached, rate limited, and budget capped."""
    return await ai_gateway.viral_insta_songs(
        niche=body.niche,
        region=body.region,
        language=body.language,
        client_key=client_ip(request),
    )


@app.get("/ai-magic/viral-insta-songs/categories")
async def viral_insta_song_categories():
    """Fixed reel-song categories. Does not call a provider."""
    return viral_browse.song_categories_payload()


@app.get("/ai-magic/viral-insta-songs/top")
async def viral_insta_songs_top(category: str):
    """Cached top songs for one category or all. Not counted against the per-user limit."""
    return await viral_browse.song_top(category)


@app.post("/ai-magic/viral-ai-art-trends")
async def viral_ai_art_trends(body: ai_gateway.ViralArtRequest, request: Request):
    """Free-tier AI image trends and original prompts. Cached, rate limited, and budget capped."""
    return await ai_gateway.viral_ai_art_trends(
        niche=body.niche,
        platform=body.platform,
        client_key=client_ip(request),
    )


@app.get("/ai-magic/viral-ai-art-trends/categories")
async def viral_ai_art_categories():
    """Fixed AI-art categories. Does not call a provider."""
    return viral_browse.art_categories_payload()


@app.get("/ai-magic/viral-ai-art-trends/browse")
async def viral_ai_art_browse(category: str):
    """Cached art trends for one category or all. Not counted against the per-user limit."""
    return await viral_browse.art_browse(category)


@app.on_event("startup")
async def start_viral_browse_refresh():
    viral_browse.maybe_start_background_refresh()


@app.on_event("shutdown")
async def shutdown_http_client():
    await viral_browse.stop_background_refresh()
    await http_client.aclose()
    await ai_gateway.aclose()
