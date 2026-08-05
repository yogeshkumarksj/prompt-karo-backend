from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc
import models, schemas

# --- CATEGORIES LOGIC ---

def get_categories(db: Session):
    return db.query(models.Category).all()

def create_category(db: Session, category: schemas.CategoryCreate):
    db_category = models.Category(name=category.name)
    db.add(db_category)
    db.commit()
    db.refresh(db_category)
    return db_category

# --- PROMPTS LOGIC (Merged & Fixed) ---

def get_prompts(db: Session, skip: int = 0, limit: int = 100, search: str = None, category: str = None, sort_by: str = None):
    query = db.query(models.Prompt)
    
    # 1. कैटेगरी फिल्टर
    if category and category != "All":
        query = query.filter(models.Prompt.category == category)
        
    # 2. सर्च फिल्टर (Title, Prompt Text, और Tags तीनों में ढूंढेगा)
    if search:
        query = query.filter(
            or_(
                models.Prompt.title.ilike(f"%{search}%"),
                models.Prompt.prompt_text.ilike(f"%{search}%"),
                models.Prompt.tags.ilike(f"%{search}%")
            )
        )

    # 3. सॉर्टिंग लॉजिक
    if sort_by == "views_high":
        query = query.order_by(desc(models.Prompt.views))
    elif sort_by == "views_low":
        query = query.order_by(asc(models.Prompt.views))
    elif sort_by == "copies_high":
        query = query.order_by(desc(models.Prompt.copies))
    elif sort_by == "shares_high":
        query = query.order_by(desc(models.Prompt.shares))
    else:
        # डिफ़ॉल्ट: नया प्रॉम्ट सबसे ऊपर
        query = query.order_by(desc(models.Prompt.id))

    return query.offset(skip).limit(limit).all()

def create_prompt(db: Session, prompt: schemas.PromptCreate):
    db_prompt = models.Prompt(**prompt.dict())
    db.add(db_prompt)
    db.commit()
    db.refresh(db_prompt)
    return db_prompt

# --- SOURCES LOGIC ---

def get_sources(db: Session):
    return db.query(models.Source).all()

def create_source(db: Session, source: schemas.SourceCreate):
    db_source = models.Source(**source.dict())
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    return db_source
