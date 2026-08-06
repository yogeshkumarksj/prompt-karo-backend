import requests
import os
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
import models, schemas, crud
from database import engine, get_db

app = FastAPI(title="Antim Prompt API")

# 1. CORS Settings (इसे सबसे ऊपर रखना ज़रूरी है)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. 'uploads' फोल्डर सेटअप
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

# --- PUBLISH PROMPT (With File Upload) ---
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
        # 1. पहले DB में एंट्री करें ताकि ID मिल जाए
        new_prompt = models.Prompt(
            title=title, prompt_text=prompt_text,
            category=category, tags=tags, image_url=""
        )
        db.add(new_prompt)
        db.commit()
        db.refresh(new_prompt)

        # 2. फाइल का नाम और कैटेगरी तैयार करें
        file_extension = file.filename.split(".")[-1]
        new_filename = f"{new_prompt.id}.{file_extension}"
        safe_category = category.replace(" ", "_").lower()

        # 3. फाइल को Hostinger पर भेजें (API Bridge)
        file_content = await file.read()
        
        response = requests.post(
            "https://antimprompt.com/upload_handler.php",
            data={
                "key": "my_private_storage_key_99", # PHP वाली की से मैच होनी चाहिए
                "category": safe_category,
                "filename": new_filename
            },
            files={"file": (new_filename, file_content)}
        )

        if response.status_code == 200:
            # 4. अगर Hostinger पर सेव हो गया, तो DB में असली URL अपडेट करें
            final_url = f"https://antimprompt.com/uploads/{safe_category}/{new_filename}"
            new_prompt.image_url = final_url
            db.commit()
            return {"status": "success", "url": final_url}
        else:
            raise HTTPException(status_code=500, detail="Failed to upload to Hostinger")

    except Exception as e:
        db.rollback()
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
