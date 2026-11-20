from sqlalchemy import Column, Integer, LargeBinary, String, Text, DateTime
from sqlalchemy.sql import func
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
    # --- ये कॉलम्स जोड़ना ज़रूरी है ---
    views = Column(Integer, default=0)
    copies = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# models.py में सबसे नीचे यह जोड़ें

class AppSetting(Base):
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String, unique=True, index=True)
    config_value = Column(String)
class Waitlist(Base):
    __tablename__ = "waitlist"
    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String)
    email = Column(String)
    whatsapp = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AiUsageCounter(Base):
    """Persisted request and token counters for the AI budget cap."""

    __tablename__ = "ai_usage_counters"
    bucket = Column(String, primary_key=True)
    amount = Column(Integer, nullable=False, default=0)


class AiResultCache(Base):
    """Cached free-feature results so repeat inputs do not call a provider."""

    __tablename__ = "ai_result_cache"
    cache_key = Column(String, primary_key=True)
    payload = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False, index=True)
    expires_at = Column(Integer, nullable=False, index=True)


class ViralBrowseCache(Base):
    """Latest songs or art browse list for one category.

    Rows stay after the refresh window so a failed refresh can still serve
    the previous list. ``cache_key`` is ``songs:<category id>`` or ``art:<id>``.
    """

    __tablename__ = "viral_browse_cache"
    cache_key = Column(String, primary_key=True)
    payload = Column(Text, nullable=False)
    generated_at = Column(Integer, nullable=False, index=True)


class ViralTemplate(Base):
    """Admin-uploaded share card. Overlay PNG bytes live in Postgres, not on disk."""

    __tablename__ = "viral_templates"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=True)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    photo_box = Column(String, nullable=False)
    text_box = Column(String, nullable=True)
    text_color = Column(String, nullable=False, default="#FFFFFF")
    overlay = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
