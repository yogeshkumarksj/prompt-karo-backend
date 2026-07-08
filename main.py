from fastapi import FastAPI
import models
from database import engine
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import models, schemas, crud
from database import SessionLocal, engine, get_db
from fastapi.middleware.cors import CORSMiddleware 


app = FastAPI(title="PromptVerse API")
models.Base.metadata.create_all(bind=engine)

# Database Tables बनाना
models.Base.metadata.create_all(bind=engine)

# 2. CORS Middleware यहाँ जोड़ें
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # इसका मतलब है किसी भी ऐप को डेटा लेने की अनुमति है
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST सब अलाउड है
    allow_headers=["*"],  # सारे हेडर्स अलाउड हैं
)

models.Base.metadata.create_all(bind=engine)

@app.get("/")
def read_root():
    return {"message": "Welcome to PromptVerse API - Day 1 Successful!"}

# --- Category Endpoints ---
@app.post("/categories/", response_model=schemas.Category)
def create_category(category: schemas.CategoryCreate, db: Session = Depends(get_db)):
    return crud.create_category(db=db, category=category)

@app.get("/categories/", response_model=List[schemas.Category])
def read_categories(db: Session = Depends(get_db)):
    return crud.get_categories(db)

# --- Prompt Endpoints ---
@app.post("/prompts/", response_model=schemas.Prompt)
def create_prompt(prompt: schemas.PromptCreate, db: Session = Depends(get_db)):
    return crud.create_prompt(db=db, prompt=prompt)

@app.get("/prompts/", response_model=List[schemas.Prompt])
def read_prompts(search: str = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return crud.get_prompts(db, skip=skip, limit=limit, search=search)