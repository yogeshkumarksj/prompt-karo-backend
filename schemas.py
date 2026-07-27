from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

# --- Category Schemas (Pydantic) ---
class CategoryBase(BaseModel):
    name: str

class CategoryCreate(CategoryBase):
    pass

class Category(CategoryBase):
    id: int
    # prompt_count: Optional[int] = 0 # अगर आप गिनती दिखाना चाहते हैं

    class Config:
        from_attributes = True

# --- Prompt Schemas (Pydantic) ---
class PromptBase(BaseModel):
    title: str
    prompt_text: str
    image_url: str
    category: str

class PromptCreate(PromptBase):
    pass

class Prompt(PromptBase):
    id: int
    views: int
    copies: int
    shares: int
    created_at: datetime

    class Config:
        from_attributes = True

# --- Source Schemas (Pydantic) ---
class SourceBase(BaseModel):
    name: str
    url: str
    type: str

class SourceCreate(SourceBase):
    pass

class Source(SourceBase):
    id: int
    is_active: bool

    class Config:
        from_attributes = True
