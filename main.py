from fastapi import FastAPI, Depends, HTTPException, Header,UploadFile, File, Depends, Form,
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
import models, schemas, crud
from database import engine, get_db
import shutil
import os

app = FastAPI(title="Antim Prompt")

# 1. CORS Settings (ब्रैकेट की गलती यहाँ ठीक कर दी गई है)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

models.Base.metadata.create_all(bind=engine)

ADMIN_KEY = "promptverse_admin_secret_786"

def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid Admin Key")
    return True

@app.get("/")
def read_root():
    return {"message": "API is Live and Working!"}

# --- PROMPTS ---
@app.get("/prompts/", response_model=List[schemas.Prompt])
def read_prompts(search: str = None, category: str = None, sort_by: str = None, db: Session = Depends(get_db)):
    return crud.get_prompts(db, search=search, category=category, sort_by=sort_by)

@app.post("/prompts/", response_model=schemas.Prompt)
def create_prompt(prompt: schemas.PromptCreate, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    return crud.create_prompt(db=db, prompt=prompt)

@app.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: int, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt: raise HTTPException(status_code=404)
    db.delete(db_prompt)
    db.commit()
    return {"message": "Deleted"}

# --- ANALYTICS (Increment View/Copy/Share) ---
@app.post("/prompts/{prompt_id}/increment/{action}")
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
    return {
        "total_prompts": total_prompts,
        "total_views": total_views,
        "total_copies": total_copies,
        "total_shares": total_shares
    }

# --- SETTINGS ---
@app.get("/settings/")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models.AppSetting).all()
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
#-----------Edit prompt-----------

@app.put("/prompts/{prompt_id}") # PUT मेथड डेटा अपडेट करने के लिए होता है
def update_prompt(prompt_id: int, prompt: schemas.PromptCreate, db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    db_prompt = db.query(models.Prompt).filter(models.Prompt.id == prompt_id).first()
    if not db_prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    
    # डेटा अपडेट करें
    db_prompt.title = prompt.title
    db_prompt.prompt_text = prompt.prompt_text
    db_prompt.image_url = prompt.image_url
    db_prompt.category = prompt.category
    
    db.commit()
    db.refresh(db_prompt)
    return db_prompt
#----------Source----------
@app.get("/sources/")
def get_sources(db: Session = Depends(get_db), authenticated: bool = Depends(verify_admin)):
    return crud.get_sources(db)

#'uploads' फोल्डर को पब्लिक करें
UPLOAD_ROOT = "uploads"
if not os.path.exists(UPLOAD_ROOT):
    os.makedirs(UPLOAD_ROOT)
app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")

@app.post("/publish-prompt")
async def publish_prompt(
    title: str = Form(...),
    prompt_text: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_admin)
):
    try:
        # 1. पहले डेटाबेस में प्रॉम्ट सेव करें (ताकि ID मिल जाए)
        new_prompt = models.Prompt(
            title=title,
            prompt_text=prompt_text,
            category=category,
            image_url="" # अभी खाली छोड़ें
        )
        db.add(new_prompt)
        db.commit()
        db.refresh(new_prompt)
        
        prompt_id = new_prompt.id

        # 2. कैटेगरी के नाम का फोल्डर बनाएं (Sanitize category name)
        safe_category = category.replace(" ", "_").lower()
        category_dir = os.path.join(UPLOAD_ROOT, safe_category)
        if not os.path.exists(category_dir):
            os.makedirs(category_dir)

        # 3. फाइल को रिनेम करें: prompt_id.extension
        file_extension = file.filename.split(".")[-1]
        file_name = f"{prompt_id}.{file_extension}"
        file_location = os.path.join(category_dir, file_name)

        # 4. फाइल को सेव करें
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)

        # 5. डेटाबेस में असली URL अपडेट करें
        # 'https://api.antimprompt.com' को अपने असली डोमेन से बदलें
        host_url = "https://prompt-karo-backend.onrender.com" 
        final_url = f"{host_url}/uploads/{safe_category}/{file_name}"
        
        new_prompt.image_url = final_url
        db.commit()

        return {"status": "success", "url": final_url}

    except Exception as e:
        db.rollback() # अगर कुछ गड़बड़ हो तो DB बदलाव वापस लें
        raise HTTPException(status_code=500, detail=str(e))
