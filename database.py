import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

# Render/Supabase URL प्राप्त करें
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

# अगर URL postgres:// से शुरू हो रहा है, तो उसे postgresql:// में बदलें (SQLAlchemy के लिए ज़रूरी)
if SQLALCHEMY_DATABASE_URL and SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

# अगर कोई URL नहीं है (लोकल टेस्टिंग के लिए)
if not SQLALCHEMY_DATABASE_URL:
    SQLALCHEMY_DATABASE_URL = "sqlite:///./promptverse.db"

# इंजन सेटअप
if "postgresql" in SQLALCHEMY_DATABASE_URL:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# --- यह लाइन सबसे ज़रूरी है (यही एरर दे रही थी) ---
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
