from pydantic import BaseModel
from typing import List, Optional

# Category Schema
class CategoryBase(BaseModel):
    name: str

class CategoryCreate(CategoryBase):
    pass

class Category(CategoryBase):
    id: int
    class Config:
        from_attributes = True

# Prompt Schema
class PromptBase(BaseModel):
    title: str
    prompt_text: str
    image_url: str
    category: int
    tags: Optional[str] = None

class PromptCreate(PromptBase):
    pass

class Prompt(PromptBase):
    id: int
    class Config:
        from_attributes = True
