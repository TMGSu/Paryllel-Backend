from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import all_routers
from app.core.database import engine  # ✅ use shared engine
from app.models.community_tag import CommunityTag  # noqa: F401 — registers mapper
from app.models.community_category import CommunityCategory  # noqa: F401
from app.models.community_join_request import CommunityJoinRequest  # noqa: F401
from app.models.notification import Notification  # noqa: F401

load_dotenv()

app = FastAPI(title="Paryllel API 🚀")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# DB check
@app.on_event("startup")
def startup():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("✅ Database connected successfully")
    except Exception as e:
        print("❌ Database connection failed:", e)


# Routers
for router in all_routers:
    app.include_router(router)


# Root
@app.get("/")
def root():
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT NOW()"))
            db_time = result.scalar()

        return {
            "status": "OK ✅",
            "message": "Backend running and DB connected 🚀",
            "database_time": str(db_time)
        }

    except SQLAlchemyError as e:
        return {
            "status": "ERROR ❌",
            "message": "Backend running but DB connection failed",
            "error": str(e)
        }