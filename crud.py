from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc
import models, schemas

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # यह सभी ओरिजिन को अनुमति देता है
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, OPTIONS सब अनुमति देता है
    allow_headers=["*"],  # सभी हेडर्स को अनुमति देता है
)

# --- CATEGORIES LOGIC ---

def get_categories(db: Session):
    """सभी कैटेगरीज की लिस्ट लाने के लिए (यही फंक्शन मिसिंग था)"""
    return db.query(models.Category).all()

def create_category(db: Session, category: schemas.CategoryCreate):
    """नई कैटेगरी बनाने के लिए"""
    db_category = models.Category(name=category.name)
    db.add(db_category)
    db.commit()
    db.refresh(db_category)
    return db_category

# --- PROMPTS LOGIC ---

def get_prompts(db: Session, skip: int = 0, limit: int = 100, search: str = None, category: str = None, sort_by: str = None):
    """प्रॉम्ट्स को फिल्टर और सॉर्ट करके लाने के लिए"""
    query = db.query(models.Prompt)
    
    # 1. कैटेगरी फिल्टर
    if category and category != "All":
        query = query.filter(models.Prompt.category == category)
        
    # 2. सर्च फिल्टर
    if search:
        query = query.filter(
            or_(
                models.Prompt.title.ilike(f"%{search}%"),
                models.Prompt.prompt_text.ilike(f"%{search}%")
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
        query = query.order_by(desc(models.Prompt.id))

    return query.offset(skip).limit(limit).all()

def create_prompt(db: Session, prompt: schemas.PromptCreate):
    """नया प्रॉम्ट डेटाबेस में डालने के लिए"""
    db_prompt = models.Prompt(**prompt.dict())
    db.add(db_prompt)
    db.commit()
    db.refresh(db_prompt)
    return db_prompt

def get_prompts(db: Session, skip: int = 0, limit: int = 100, search: str = None, category: str = None, sort_by: str = None):
    query = db.query(models.Prompt)
    
    if category and category != "All":
        query = query.filter(models.Prompt.category == category)
        
    if search:
        # अब यह Title, Prompt Text और TAGS तीनों में ढूंढेगा
        query = query.filter(
            or_(
                models.Prompt.title.ilike(f"%{search}%"),
                models.Prompt.prompt_text.ilike(f"%{search}%"),
                models.Prompt.tags.ilike(f"%{search}%") # <--- यह लाइन जोड़ें
            )
        )
        
# --- SOURCES LOGIC ---

def get_sources(db: Session):
    """स्क्रैपिंग सोर्सेज की लिस्ट लाने के लिए"""
    return db.query(models.Source).all()

def create_source(db: Session, source: schemas.SourceCreate):
    """नया सोर्स जोड़ने के लिए"""
    db_source = models.Source(**source.dict())
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    return db_source
