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
from sqlalchemy import Column, Integer, String, Text
from database import Base

class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)


class Prompt(Base):
    __tablename__ = "prompts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    prompt_text = Column(Text)
    image_url = Column(String)
    category = Column(String, default="General")
    tags = Column(String, nullable=True)

class PromptCreate(PromptBase):
    pass

class Prompt(PromptBase):
    id: int
    class Config:
        from_attributes = True
