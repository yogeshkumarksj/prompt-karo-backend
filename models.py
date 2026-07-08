from sqlalchemy import Column, Integer, String, ForeignKey, Text
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
    category_id = Column(Integer, ForeignKey("categories.id"))
    tags = Column(String) # "anime,4k,warrior"