from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware 
from sqlalchemy.orm import Session
from typing import List
import models, schemas, crud
from database import engine, get_db

app = FastAPI(title="PromptVerse API")

# CORS Middleware (इसे सबसे ऊपर रखें)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database Tables बनाना
models.Base.metadata.create_all(bind=engine)

@app.get("/")
def read_root():
    return {"message": "Welcome to PromptVerse API - Live!"}

# --- Category Endpoints ---
@app.get("/categories/", response_model=List[schemas.Category])
def read_categories(db: Session = Depends(get_db)):
    return crud.get_categories(db)

@app.post("/categories/", response_model=schemas.Category)
def create_category(category: schemas.CategoryCreate, db: Session = Depends(get_db)):
    return crud.create_category(db=db, category=category)

# --- Prompt Endpoints ---
@app.get("/prompts/", response_model=List[schemas.Prompt])
def read_prompts(search: str = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return crud.get_prompts(db, skip=skip, limit=limit, search=search)

@app.post("/prompts/", response_model=schemas.Prompt)
def create_prompt(prompt: schemas.PromptCreate, db: Session = Depends(get_db)):
    return crud.create_prompt(db=db, prompt=prompt)
