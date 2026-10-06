"""
config.py
---------
Single place for application-wide settings. Kept as plain Python (no
extra dependency like pydantic-settings) since the requirement list for
this milestone is FastAPI + Jinja2 + SQLite + SQLAlchemy + Uvicorn only.
"""

from pathlib import Path


class Settings:
    APP_NAME: str = "SmartQuiz"

    BASE_DIR: Path = Path(__file__).resolve().parent
    TEMPLATES_DIR: Path = BASE_DIR / "templates"

    DATABASE_FILE: Path = BASE_DIR / "smartquiz.db"
    DATABASE_URL: str = f"sqlite:///{DATABASE_FILE}"


settings = Settings()
