"""
Papers router — manages paper_data collection in MongoDB

paper_id format (21 digits):
  000000  = 6-digit user id
  1       = 1-digit OMR code
  09/11   = 2-digit class (09=SSC, 11=HSC)
  173     = 3-digit subject code (zero-padded)
  1/2     = 1-digit version (1=Bangla, 2=English)
  0000001 = 7-digit serial from DB (auto-incremented)
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Optional
from database import get_db

router = APIRouter()

COLLECTION = "paper_data"
COUNTER_COLLECTION = "paper_counters"  # stores serial numbers per subject


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _next_serial(db, class_slug: str, subject_code: int, lang: str) -> int:
    """Atomically increment and return the next serial for a class+subject+lang."""
    key = f"{class_slug}_{subject_code}_{lang}"
    result = await db[COUNTER_COLLECTION].find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return result["seq"]


def _build_paper_id(
    user_id: str,
    omr_code: int,
    class_slug: str,
    subject_code: int,
    lang: str,
    serial: int,
) -> str:
    user_part    = user_id.zfill(6)[:6]
    omr_part     = str(omr_code)
    class_part   = "11" if class_slug == "hsc" else "09"
    subject_part = str(subject_code).zfill(3)
    version_part = "2" if lang == "en" else "1"
    serial_part  = str(serial).zfill(7)
    return f"{user_part}{omr_part}{class_part}{subject_part}{version_part}{serial_part}"


# ── Request / Response models ─────────────────────────────────────────────────

class CreatePaperRequest(BaseModel):
    user_id:       str = "000000"
    class_slug:    str                    # "ssc" | "hsc"
    subject_code:  int                    # e.g. 173
    lang:          str = "bn"            # "bn" | "en"
    subject_codes: Dict[str, List[int]]  # { "ssc_Bangla_173_2": [] }
    omr_code:      int = 1

class UpdatePaperRequest(BaseModel):
    subject_codes:    Dict[str, List[int]]  # { code: [questionNo, ...] }
    ans_sequence:     List[int]             # encoded answer digits
    institution_name: Optional[str] = None
    duration:         Optional[int] = None
    subject:          Optional[str] = None  # human label e.g. "Physics"
    class_label:      Optional[str] = None  # display label e.g. "Nine"
    exam_name:        Optional[str] = None  # e.g. "অর্ধবার্ষিক পরীক্ষা"
    exam_date:        Optional[str] = None  # e.g. "27/05/2026"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/create", status_code=201)
async def create_paper(req: CreatePaperRequest):
    """
    Called when user presses 'Create Paper' in create_question.dart.
    Generates a unique paper_id and saves an empty paper_data document.
    """
    db = get_db()

    serial   = await _next_serial(db, req.class_slug, req.subject_code, req.lang)
    paper_id = _build_paper_id(
        req.user_id, req.omr_code, req.class_slug,
        req.subject_code, req.lang, serial,
    )

    doc = {
        "paper_id":         paper_id,
        "created_at":       datetime.now(timezone.utc),
        "created_by":       req.user_id,
        "subject_codes":    req.subject_codes,
        "ans_sequence":     [],
        "omr_image_online": f"www.proshnopotro.com/result/{paper_id}",
        "omr_image_local":  "",
        "scan_info_list":   [],
        "correct":          None,
        "incorrect":        None,
        "skipped":          None,
    }

    await db[COLLECTION].insert_one(doc)
    return {"paper_id": paper_id, "message": "Paper created"}


@router.patch("/{paper_id}")
async def update_paper(paper_id: str, req: UpdatePaperRequest):
    """
    Called when user presses 'PDF' in question_selector.dart.

    Merges incoming subject_codes INTO the existing ones using dot-notation $set.
    This preserves codes that have no questions selected (empty lists stay).
    Only codes that have selected questions are updated — others are untouched.
    """
    db = get_db()

    existing = await db[COLLECTION].find_one({"paper_id": paper_id})
    if not existing:
        raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

    # Build dot-notation update for each code individually so other codes are preserved.
    # e.g. { "subject_codes.ssc_Bangla_173_2": [1,3,7], "ans_sequence": [...] }
    set_payload: dict = {"ans_sequence": req.ans_sequence}
    for code, question_nos in req.subject_codes.items():
        set_payload[f"subject_codes.{code}"] = sorted(question_nos)
    if req.institution_name is not None:
        set_payload["institution_name"] = req.institution_name
    if req.duration is not None:
        set_payload["duration"] = req.duration
    if req.subject is not None:
        set_payload["subject"] = req.subject
    if req.class_label is not None:
        set_payload["class_label"] = req.class_label
    if req.exam_name is not None:
        set_payload["exam_name"] = req.exam_name
    if req.exam_date is not None:
        set_payload["exam_date"] = req.exam_date

    await db[COLLECTION].update_one(
        {"paper_id": paper_id},
        {"$set": set_payload},
        upsert=False,
    )

    return {"message": "Paper updated", "paper_id": paper_id}


@router.get("/list")
async def list_papers(user_id: str = "000000"):
    """Return all completed papers (ans_sequence non-empty) for a user."""
    db = get_db()
    cursor = db[COLLECTION].find(
        {"created_by": user_id, "ans_sequence": {"$not": {"$size": 0}}},
        {
            "_id": 0,
            "paper_id": 1,
            "created_at": 1,
            "institution_name": 1,
            "duration": 1,
            "subject": 1,
            "class_label": 1,
            "ans_sequence": 1,
        },
    ).sort("created_at", -1)
    papers = []
    async for doc in cursor:
        doc["created_at"] = doc["created_at"].isoformat()
        doc["mcq_count"] = len(doc.pop("ans_sequence", []))
        papers.append(doc)
    return {"papers": papers}


@router.get("/{paper_id}")
async def get_paper(paper_id: str):
    """Fetch a paper by its paper_id."""
    db  = get_db()
    doc = await db[COLLECTION].find_one({"paper_id": paper_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

    doc["created_at"] = doc["created_at"].isoformat()
    return doc


# ── Evaluate ──────────────────────────────────────────────────────────────────

class EvaluatePaperRequest(BaseModel):
    scanned_by:          str
    qr_value:            str
    roll:                str
    reg:                 str
    ans_bubble_sequence: List[int]
    correct:             int
    incorrect:           int
    skipped:             int


@router.post("/{paper_id}/evaluate")
async def evaluate_paper(paper_id: str, req: EvaluatePaperRequest):
    """
    Append one scan entry and update correct/incorrect/skipped counts.
    Maximum 4 evaluations per paper.
    """
    db  = get_db()
    doc = await db[COLLECTION].find_one({"paper_id": paper_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

    scan_count = len(doc.get("scan_info_list", []))
    if scan_count >= 4:
        raise HTTPException(
            status_code=400,
            detail="Maximum 4 evaluations allowed per paper",
        )

    scan_info = {
        "scanned_at":        datetime.now(timezone.utc).isoformat(),
        "scanned_by":        req.scanned_by,
        "qr_value":          req.qr_value,
        "roll":              req.roll,
        "reg":               req.reg,
        "ans_bubble_sequence": req.ans_bubble_sequence,
    }

    await db[COLLECTION].update_one(
        {"paper_id": paper_id},
        {
            "$push": {"scan_info_list": scan_info},
            "$set": {
                "correct":   req.correct,
                "incorrect": req.incorrect,
                "skipped":   req.skipped,
            },
        },
    )
    return {"message": "Evaluation saved", "paper_id": paper_id}
