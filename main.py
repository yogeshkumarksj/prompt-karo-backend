import shutil
import os
import time
import io  # <--- यह मिसिंग था
import requests
from PIL import Image # <--- यह मिसिंग था
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
import models, schemas, crud
from database import engine, get_db

INSTA_LOOTER_KEY = "आपकी_असली_RAPIDAPI_KEY_यहाँ_डालें" 
INSTA_LOOTER_HOST = "instagram-looter2.p.rapidapi.com"

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
    new_entry = models.Waitlist(**data.dict())
    db.add(new_entry)
    db.commit()
    return {"message": "Success"}


@app.get("/ai-magic/insta-audit/{username}")
async def insta_audit(username: str):
    # 1. RapidAPI से Instagram डेटा मंगाना
    url = "https://instagram-looter2.p.rapidapi.com/user/info"
    querystring = {"username": username}
    headers = {
        "x-rapidapi-key": INSTA_LOOTER_KEY,
        "x-rapidapi-host": INSTA_LOOTER_HOST
    }

    try:
        response = requests.get(url, headers=headers, params=querystring)
        data = response.json()

        # चेक करें कि क्या API ने डेटा दिया
        if not data.get("success") or "data" not in data:
            raise HTTPException(status_code=404, detail="Instagram user not found")

        user_info = data["data"]

        # --- CUSTOM PRIVACY CHECK (सबसे ज़रूरी) ---
        # अगर अकाउंट प्राइवेट है, तो यहीं से वापस लौट जाओ
        if user_info.get('is_private') == True:
            return {
                "status": "private_account",
                "message": "This account is private. Please make your account public first to get an AI Audit report."
            }
        # ----------------------------------------

        # 2. अगर अकाउंट पब्लिक है, तभी Gemini AI को डेटा भेजें
        raw_data_for_ai = f"""
        Profile Name: {user_info.get('full_name')}
        Bio: {user_info.get('biography')}
        Followers: {user_info.get('follower_count')}
        Following: {user_info.get('following_count')}
        Total Posts: {user_info.get('media_count')}
        """

        ai_prompt = f"""
        You are an Instagram Growth Expert. Analyze this profile data:
        {raw_data_for_ai}
        Provide a growth audit report in STRICT JSON format with:
        "score", "bio_analysis", "bio_suggestion", "content_strategy", "growth_tip".
        """

        model = genai.GenerativeModel('gemini-1.5-flash')
        ai_response = model.generate_content(ai_prompt)
        
        clean_json = ai_response.text.replace('```json', '').replace('```', '').strip()
        report = json.loads(clean_json)

        return {
            "status": "success",
            "username": username,
            "profile_pic": user_info.get('profile_pic_url'),
            "report": report
        }

    except Exception as e:
        print(f"Audit Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate AI Audit")
