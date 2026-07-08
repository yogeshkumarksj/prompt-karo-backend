from sqlalchemy.orm import Session
import models, schemas

# Categories के लिए
def get_categories(db: Session):
    return db.query(models.Category).all()

def create_category(db: Session, category: schemas.CategoryCreate):
    db_category = models.Category(name=category.name)
    db.add(db_category)
    db.commit()
    db.refresh(db_category)
    return db_category

# Prompts के लिए (Search Logic के साथ)
def get_prompts(db: Session, skip: int = 0, limit: int = 100, search: str = None):
    query = db.query(models.Prompt)
    if search:
        # Title या Tags में सर्च करेगा
        query = query.filter(
            (models.Prompt.title.ilike(f"%{search}%")) | 
            (models.Prompt.tags.ilike(f"%{search}%"))
        )
    return query.offset(skip).limit(limit).all()

def create_prompt(db: Session, prompt: schemas.PromptCreate):
    db_prompt = models.Prompt(**prompt.dict())
    db.add(db_prompt)
    db.commit()
    db.refresh(db_prompt)
    return db_prompt