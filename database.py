from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

# अभी के लिए हम SQLite इस्तेमाल कर सकते हैं, बाद में PostgreSQL में बदल देंगे
SQLALCHEMY_DATABASE_URL = "sqlite:///./promptverse.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Database Session पाने के लिए function
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()