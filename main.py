import shutil
import os
import time
import io
import requests
from PIL import Image ा
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from pydantic import BaseModel, Field
import models, schemas, crud
from database import engine, get_db
import json
from google import genai

INSTA_LOOTER_HOST = "instagram-looter2.p.rapidapi.com"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = FastAPI(title="Antim Prompt API")

# 1. CORS Settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. 'uploads' फोल्डर सेटअप (लोकल बैकअप के लिए)
UPLOAD_ROOT = "uploads"
if not os.path.exists(UPLOAD_ROOT):
    os.makedirs(UPLOAD_ROOT)

app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")

models.Base.metadata.create_all(bind=engine)

ADMIN_KEY = "promptverse_admin_secret_786"

def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid Admin Key")
    return True

@app.get("/")
def read_root():
    return {"message": "Antim Prompt API is Live!"}

# --- PUBLISH PROMPT (Fixed with correct Imports & Host URL) ---
@app.post("/publish-prompt/")
async def publish_prompt(
    title: str = Form(...),
    prompt_text: str = Form(...),
    category: str = Form(...),
    tags: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin)
):
    try:
        # 1. पहले डेटाबेस में एंट्री करें
        new_prompt = models.Prompt(
            title=title, prompt_text=prompt_text,
            category=category, tags=tags, image_url=""
        )
        db.add(new_prompt)
        db.commit()
        db.refresh(new_prompt)

        # 2. इमेज को WebP में बदलें
        file_content = await file.read()
        img = Image.open(io.BytesIO(file_content)).convert("RGB")
        webp_buffer = io.BytesIO()
        img.save(webp_buffer, format="WEBP", quality=95)
        
        # 3. फाइल का नाम (ID.webp)
        new_filename = f"{new_prompt.id}.webp"
        safe_category = category.strip().replace(" ", "_").lower()

        # 4. Hostinger PHP Bridge को भेजें
        php_url = "https://antimprompt.com/upload_handler.php" 
        
        response = requests.post(
            php_url,
            data={
                "key": "my_private_storage_key_99", 
                "category": safe_category,
                "filename": new_filename
            },
            files={"file": (new_filename, webp_buffer.getvalue(), "image/webp")}
        )

        if response.status_code == 200:
            # 5. DB में Final URL अपडेट करें (अब Hostinger का लिंक सेव होगा)
            host_url = "https://antimprompt.com" 
            final_url = f"{host_url}/uploads/{safe_category}/{new_filename}"
            
            new_prompt.image_url = final_url
            db.commit()
            return {"status": "success", "url": final_url}
        else:
            db.delete(new_prompt)
            db.commit()
            raise HTTPException(status_code=500, detail="Hostinger PHP Bridge Error")

    except Exception as e:
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
    if not db_prompt: raise HTTPException(status_code=404)
    db.delete(db_prompt)
    db.commit()
    return {"message": "Deleted"}

@app.put("/prompts/{prompt_id}/", response_model=schemas.Prompt)
def update_prompt(prompt_id: int, prompt: schemas.PromptCreate, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt: raise HTTPException(status_code=404)
    db_prompt.title = prompt.title
    db_prompt.prompt_text = prompt.prompt_text
    db_prompt.image_url = prompt.image_url
    db_prompt.category = prompt.category
    db.commit()
    db.refresh(db_prompt)
    return db_prompt

# --- ANALYTICS ---
@app.post("/prompts/{prompt_id}/increment/{action}/")
def increment_stat(prompt_id: int, action: str, db: Session = Depends(get_db)):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt: raise HTTPException(status_code=404)
    if action == "view": db_prompt.views += 1
    elif action == "copy": db_prompt.copies += 1
    elif action == "share": db_prompt.shares += 1
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
def create_category(category: schemas.CategoryCreate, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    return crud.create_category(db=db, category=category)

# --- ADMIN STATS ---
@app.get("/admin/stats/")
def get_admin_stats(db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    total_prompts = db.query(models.Prompt).count()
    total_views = db.query(func.sum(models.Prompt.views)).scalar() or 0
    total_copies = db.query(func.sum(models.Prompt.copies)).scalar() or 0
    total_shares = db.query(func.sum(models.Prompt.shares)).scalar() or 0
    return {"total_prompts": total_prompts, "total_views": total_views, "total_copies": total_copies, "total_shares": total_shares}

# --- SETTINGS ---
@app.get("/settings/")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models.AppSetting).all()
    if not settings: return {"timer_enabled": "false", "timer_seconds": "5"}
    return {s.config_key: s.config_value for s in settings}

@app.post("/settings/")
def update_setting(key: str, value: str, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    setting = db.query(models.AppSetting).filter(models.AppSetting.config_key == key).first()
    if setting: setting.config_value = value
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
# TEMPORARY API KEYS - LOCAL TESTING ONLY
# =========================================================

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY") or os.getenv("INSTA_LOOTER_KEY")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =========================================================

RAPIDAPI_HOST = "instagram-looter2.p.rapidapi.com"
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/profile"

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ---------------------------------------------------------
# Gemini output schema
# ---------------------------------------------------------

class AuditScore(BaseModel):
    score: int = Field(ge=0, le=100)
    reason: str


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

    summary: str


# ---------------------------------------------------------
# RapidAPI
# ---------------------------------------------------------

def get_instagram_profile(username: str) -> dict:

    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }

    params = {
        "username": username
    }

    try:
        response = requests.get(
            RAPIDAPI_URL,
            headers=headers,
            params=params,
            timeout=30,
        )

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"RapidAPI request failed: {str(exc)}",
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"RapidAPI returned HTTP {response.status_code}",
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="RapidAPI returned invalid JSON",
        )

    return data


# ---------------------------------------------------------
# Extract profile metrics
# ---------------------------------------------------------

def extract_profile_metrics(data: dict) -> dict:

    return {
        "username": data.get("username"),
        "full_name": data.get("full_name"),
        "biography": data.get("biography"),

        "followers": data.get(
            "edge_followed_by", {}
        ).get("count", 0),

        "following": data.get(
            "edge_follow", {}
        ).get("count", 0),

        "total_posts": data.get(
            "edge_owner_to_timeline_media", {}
        ).get("count", 0),

        "is_private": data.get("is_private", False),
        "is_verified": data.get("is_verified", False),
        "is_professional_account": data.get(
            "is_professional_account", False
        ),
        "is_business_account": data.get(
            "is_business_account", False
        ),

        "profile_pic_url": (
            data.get("profile_pic_url_hd")
            or data.get("profile_pic_url")
        ),
    }


# ---------------------------------------------------------
# Extract last 10 posts
# ---------------------------------------------------------

def extract_recent_posts(data: dict, limit: int = 10) -> list:

    media = data.get(
        "edge_owner_to_timeline_media",
        {}
    )

    edges = media.get("edges", [])

    posts = []

    for edge in edges[:limit]:

        post = edge.get("node", {})

        caption_edges = (
            post.get("edge_media_to_caption", {})
            .get("edges", [])
        )

        caption = ""

        if caption_edges:
            caption = (
                caption_edges[0]
                .get("node", {})
                .get("text", "")
            )

        likes = (
            post.get("edge_liked_by", {})
            .get("count", 0)
        )

        comments = (
            post.get("edge_media_to_comment", {})
            .get("count", 0)
        )

        video_views = post.get(
            "video_view_count",
            0
        ) or 0

        posts.append({
            "id": post.get("id"),
            "shortcode": post.get("shortcode"),
            "type": post.get("__typename"),

            "caption": caption,

            "likes": likes,
            "comments": comments,
            "video_views": video_views,

            "timestamp": post.get(
                "taken_at_timestamp"
            ),

            "is_video": post.get(
                "is_video",
                False
            ),
        })

    return posts


# ---------------------------------------------------------
# Engagement calculation
# ---------------------------------------------------------

def calculate_engagement(
    followers: int,
    posts: list
) -> dict:

    if not posts:
        return {
            "average_likes": 0,
            "average_comments": 0,
            "average_video_views": 0,
            "average_engagement_rate": 0,
            "total_engagement": 0,
        }

    total_likes = sum(
        post["likes"]
        for post in posts
    )

    total_comments = sum(
        post["comments"]
        for post in posts
    )

    total_views = sum(
        post["video_views"]
        for post in posts
    )

    post_count = len(posts)

    average_likes = total_likes / post_count
    average_comments = total_comments / post_count
    average_views = total_views / post_count

    if followers > 0:

        engagement_rates = []

        for post in posts:

            engagement = (
                post["likes"]
                + post["comments"]
            )

            rate = (
                engagement / followers
            ) * 100

            engagement_rates.append(rate)

        average_engagement_rate = (
            sum(engagement_rates)
            / len(engagement_rates)
        )

    else:
        average_engagement_rate = 0

    return {
        "average_likes": round(
            average_likes,
            2
        ),

        "average_comments": round(
            average_comments,
            2
        ),

        "average_video_views": round(
            average_views,
            2
        ),

        "average_engagement_rate": round(
            average_engagement_rate,
            4
        ),

        "total_engagement": (
            total_likes
            + total_comments
        ),
    }


# ---------------------------------------------------------
# Gemini audit
# ---------------------------------------------------------

def generate_ai_audit(
    profile: dict,
    posts: list,
    engagement: dict,
) -> AuditReport:

    audit_data = {
        "profile": profile,
        "recent_posts": posts,
        "engagement": engagement,
    }

    prompt = f"""
You are an expert Instagram growth and content strategist.

Analyze the following Instagram account data.

IMPORTANT:
- Use ONLY the supplied data.
- Do not invent metrics.
- Do not claim access to information that is not supplied.
- Give practical recommendations.
- Evaluate profile optimization, content quality,
  engagement, consistency and growth potential.

Instagram data:

{audit_data}

Return a complete Instagram audit.

Focus on:

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

Return ONLY structured JSON matching the requested schema.
"""

    try:

        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": AuditReport,
            },
        )

        if response.parsed:
            return response.parsed

        return AuditReport.model_validate_json(
            response.text
        )

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=f"Gemini audit generation failed: {str(exc)}",
        )


# ---------------------------------------------------------
# FINAL API
# ---------------------------------------------------------

@app.get("/ai-magic/insta-audit/{username}")
def instagram_audit(username: str):

    username = username.strip().lstrip("@")

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Instagram username is required"
        )

    if not RAPIDAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail="RAPIDAPI_KEY is not configured"
        )

    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not configured"
        )

    # 1. RapidAPI
    raw_profile = get_instagram_profile(
        username
    )

    # Check API response
    if raw_profile.get("status") is False:

        raise HTTPException(
            status_code=404,
            detail="Instagram profile not found"
        )

    # 2. Profile metrics
    profile = extract_profile_metrics(
        raw_profile
    )

    # 3. Last 10 posts
    recent_posts = extract_recent_posts(
        raw_profile,
        limit=10
    )

    # 4. Engagement
    engagement = calculate_engagement(
        followers=profile["followers"],
        posts=recent_posts
    )

    # 5. Gemini
    audit = generate_ai_audit(
        profile=profile,
        posts=recent_posts,
        engagement=engagement,
    )

    # 6. Final JSON
    return {
        "status": "success",

        "username": username,

        "profile": profile,

        "recent_posts": recent_posts,

        "engagement": engagement,

        "ai_audit": audit.model_dump(),
    }
