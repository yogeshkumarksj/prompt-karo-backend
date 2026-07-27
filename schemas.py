from pydantic import BaseModel
from typing import Optional

# -------------------------
# Category Schemas
# -------------------------

class CategoryBase(BaseModel):
    name: str


class CategoryCreate(CategoryBase):
    pass


class Category(CategoryBase):
    id: int

    class Config:
        from_attributes = True


# -------------------------
# Prompt Schemas
# -------------------------

class PromptBase(BaseModel):
    title: str
    prompt_text: str
    image_url: str
    category: str
    tags: Optional[str] = None


class PromptCreate(PromptBase):
    pass


class Prompt(PromptBase):
    id: int

    class Config:
        from_attributes = True
