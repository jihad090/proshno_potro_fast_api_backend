from dotenv import load_dotenv
load_dotenv()   # must run before any module that calls os.getenv at import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from routers import questions, papers
from database import connect_db, close_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()

app = FastAPI(
    title="Question Bank API",
    description="SSC bilingual MCQ bank — Flutter compatible",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(questions.router, prefix="/api/questions", tags=["Questions"])
app.include_router(papers.router,    prefix="/api/papers",    tags=["Papers"])

@app.get("/")
async def root():
    return {"status": "ok", "message": "Question Bank API is running"}