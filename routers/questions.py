"""
MongoDB code format:  ssc_{version}_{subjectID}_{chapterNo}
  e.g.  ssc_Bangla_173_2
        ssc_English_173_2

Compound index: (code, questionNo)
"""

from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from database import get_db, COLLECTION
from models import BankQuestionResponse, QuestionListResponse

router = APIRouter()

VERSION_MAP = {"bn": "Bangla", "en": "English"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_code(class_id: str, version: str, subject_id: str, chapter_no: str) -> str:
    """e.g. ("ssc","Bangla","173","2") → "ssc_Bangla_173_2" """
    return f"{class_id.lower()}_{version}_{subject_id}_{chapter_no}"


def _transform(doc: dict, subject_label: str) -> dict:
    """Raw MongoDB doc → Flutter BankQuestion JSON shape."""
    ans_idx = 0
    flutter_options = []

    # Only expose the first 4 options (A–D) to Flutter.
    # A 5th option ("সঠিক উত্তর নাই") exists in some questions but is not a
    # valid MCQ answer — including it sets ans_idx=4 which causes a negative
    # bit-shift crash in Flutter's PDF generator (1 << (3 - 4) = 1 << -1).
    for i, option_group in enumerate(doc.get("options", [])[:4]):
        group_out = []
        for item in option_group:
            if item.get("isCorrectOption", False):
                ans_idx = i
            group_out.append({
                "text":      item.get("text") or "",
                "imageLink": item.get("imageLink"),
            })
        flutter_options.append(group_out)

    parts      = doc.get("code", "").split("_")   # ["ssc","Bangla","173","2"]
    class_id   = parts[0].upper() if len(parts) > 0 else "SSC"
    chapter_no = parts[3]         if len(parts) > 3 else ""

    return {
        "id":                doc.get("questionNo", 0),
        "classId":           class_id,
        "subject":           subject_label,
        "chapterName":       chapter_no,
        "questionStatement": [
            {"text": s.get("text") or "", "imageLink": s.get("imageLink")}
            for s in doc.get("questionStatement", [])
        ],
        "options":   flutter_options,
        "ansIdx":    ans_idx,
        "difficulty": doc.get("difficulty", "easy"),
    }


# ── /codes  (debug — see what's actually in MongoDB) ─────────────────────────
# NOTE: all named sub-routes MUST come before /{question_no}

@router.get("/codes")
async def list_codes():
    """
    Returns every distinct `code` value in the collection.
    Run this first to verify your MongoDB codes match what Flutter sends.
    GET /api/questions/codes
    """
    db     = get_db()
    codes  = await db[COLLECTION].distinct("code")
    sample = await db[COLLECTION].find_one({}, {"code": 1, "questionNo": 1, "_id": 0})
    total_docs = await db[COLLECTION].count_documents({})
    return {
        "total_documents": total_docs,
        "total_codes":     len(codes),
        "codes":           sorted(codes),
        "sample_doc":      sample,
    }


# ── /meta ─────────────────────────────────────────────────────────────────────

@router.get("/meta")
async def get_meta(
    class_id:   str = Query("ssc", example="ssc"),
    subject_id: str = Query(...,   example="173"),
    chapter_no: str = Query(...,   example="2"),
):
    """Counts + difficulty breakdown for both Bangla and English versions."""
    db = get_db()

    bn_code = _build_code(class_id, "Bangla",  subject_id, chapter_no)
    en_code = _build_code(class_id, "English", subject_id, chapter_no)

    async def breakdown(code: str) -> dict:
        pipeline = [
            {"$match": {"code": code}},
            {"$group": {"_id": "$difficulty", "count": {"$sum": 1}}}
        ]
        rows  = await db[COLLECTION].aggregate(pipeline).to_list(length=10)
        total = sum(r["count"] for r in rows)
        return {"total": total, "difficulty": {r["_id"]: r["count"] for r in rows}}

    bn = await breakdown(bn_code)
    en = await breakdown(en_code)

    return {
        "classId":   class_id.upper(),
        "subjectId": subject_id,
        "chapterNo": chapter_no,
        "bangla":  {"code": bn_code, "available": bn["total"] > 0, **bn},
        "english": {"code": en_code, "available": en["total"] > 0, **en},
    }


# ── /by-ids ───────────────────────────────────────────────────────────────────

@router.get("/by-ids", response_model=QuestionListResponse)
async def get_questions_by_ids(
    class_id:      str = Query("ssc",  example="ssc"),
    subject_id:    str = Query(...,    example="173"),
    chapter_no:    str = Query(...,    example="2"),
    lang:          str = Query("bn",   pattern="^(bn|en)$"),
    ids:           str = Query(...,    description="Comma-separated questionNo values"),
    subject_label: str = Query("Physics"),
):
    """Fetch specific questions by questionNo — used for PDF generation."""
    code = _build_code(class_id, VERSION_MAP.get(lang, "Bangla"), subject_id, chapter_no)

    try:
        nos = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers")

    db   = get_db()
    docs = await db[COLLECTION].find(
        {"code": code, "questionNo": {"$in": nos}}
    ).sort("questionNo", 1).to_list(length=200)

    return {"total": len(docs), "count": len(docs),
            "questions": [_transform(d, subject_label) for d in docs]}


# ── / — main list ─────────────────────────────────────────────────────────────

@router.get("/", response_model=QuestionListResponse)
async def get_questions(
    class_id:      str           = Query("ssc",    example="ssc"),
    subject_id:    str           = Query(...,       example="173"),
    chapter_no:    str           = Query(...,       example="2"),
    lang:          str           = Query("bn",      pattern="^(bn|en)$"),
    subject_label: str           = Query("Physics"),
    difficulty:    Optional[str] = Query(None,      pattern="^(easy|medium|hard)$"),
    search:        Optional[str] = Query(None),
    limit:         int           = Query(100, ge=1, le=200),
    skip:          int           = Query(0,   ge=0),
):
    """
    Fetch questions for a chapter + language.
    Builds MongoDB code: {class_id}_{version}_{subject_id}_{chapter_no}
    e.g. lang=bn → ssc_Bangla_173_2
    """
    code  = _build_code(class_id, VERSION_MAP.get(lang, "Bangla"), subject_id, chapter_no)
    query: dict = {"code": code}

    if difficulty:
        query["difficulty"] = difficulty
    if search:
        query["questionStatement"] = {
            "$elemMatch": {"text": {"$regex": search, "$options": "i"}}
        }

    db     = get_db()
    total  = await db[COLLECTION].count_documents(query)
    cursor = db[COLLECTION].find(query).sort("questionNo", 1).skip(skip).limit(limit)
    docs   = await cursor.to_list(length=limit)

    return {
        "total":     total,
        "count":     len(docs),
        "questions": [_transform(d, subject_label) for d in docs],
    }


# ── /{question_no} — MUST be last ────────────────────────────────────────────

@router.get("/{question_no}", response_model=BankQuestionResponse)
async def get_question_by_no(
    question_no:   int,
    subject_label: str = Query("Physics"),
):
    """Fetch a single question by its questionNo."""
    db  = get_db()
    doc = await db[COLLECTION].find_one({"questionNo": question_no})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Question {question_no} not found")
    return _transform(doc, subject_label)