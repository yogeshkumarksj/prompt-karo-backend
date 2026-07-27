# database.py
import os
from sqlalchemy import create_engine

# URL को Environment Variable से लें
url = os.getenv("DATABASE_URL")

# अगर लिंक postgres:// से शुरू हो रहा है, तो उसे postgresql:// में बदलें
if url and url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

SQLALCHEMY_DATABASE_URL = url

# इंजन बनाएं
engine = create_engine(SQLALCHEMY_DATABASE_URL)
