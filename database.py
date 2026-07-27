import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

# Render पर हम DATABASE_URL नाम का Environment Variable सेट करेंगे
# अगर वो नहीं मिलता, तो ही SQLite इस्तेमाल होगा
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if SQLALCHEMY_DATABASE_URL is None:
    # लोकल टेस्टिंग के लिए
    SQLALCHEMY_DATABASE_URL = "sqlite:///./promptverse.db"

# PostgreSQL के लिए अलग सेटिंग्स और SQLite के लिए अलग
if SQLALCHEMY_DATABASE_URL.startswith("postgresql"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
