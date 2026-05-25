from pydantic import BaseModel
from typing import Optional, List


class RichContent(BaseModel):
    text: str
    imageLink: Optional[str] = None


class BankQuestionResponse(BaseModel):
    id: int
    classId: str
    subject: str
    chapterName: str
    questionStatement: List[RichContent]
    options: List[List[RichContent]]
    ansIdx: int
    difficulty: Optional[str] = "easy"


class QuestionListResponse(BaseModel):
    total: int
    count: int
    questions: List[BankQuestionResponse]