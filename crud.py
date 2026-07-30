from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc
import models, schemas

def get_prompts(db: Session, skip: int = 0, limit: int = 100, search: str = None, category: str = None, sort_by: str = None):
    query = db.query(models.Prompt)
    
    # 1. कैटेगरी फिल्टर (अगर 'All' नहीं है)
    if category and category != "All":
        query = query.filter(models.Prompt.category == category)
        
    # 2. सर्च फिल्टर (Title या Prompt Text में)
    if search:
        query = query.filter(
            or_(
                models.Prompt.title.ilike(f"%{search}%"),
                models.Prompt.prompt_text.ilike(f"%{search}%")
            )
        )

    # 3. सॉर्टिंग (Sorting) लॉजिक
    if sort_by == "views_high":
        query = query.order_by(desc(models.Prompt.views))
    elif sort_by == "views_low":
        query = query.order_by(asc(models.Prompt.views))
    elif sort_by == "copies_high":
        query = query.order_by(desc(models.Prompt.copies))
    elif sort_by == "shares_high":
        query = query.order_by(desc(models.Prompt.shares))
    else:
        # डिफ़ॉल्ट: नया प्रॉम्ट सबसे ऊपर (Newest First)
        query = query.order_by(desc(models.Prompt.id))

    return query.offset(skip).limit(limit).all()

# बाकी के फंक्शन्स (create_prompt, get_categories आदि) नीचे वैसे ही रहेंगे...
