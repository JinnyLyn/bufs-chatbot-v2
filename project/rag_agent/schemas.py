from typing import List
from pydantic import BaseModel, Field

class QueryAnalysis(BaseModel):
    is_clear: bool = Field(
        description="사용자 질문이 명확하고 답변 가능한지 여부입니다."
    )
    questions: List[str] = Field(
        description="문서 검색에 적합하도록 재작성한 독립적인 질문 목록입니다."
    )
    clarification_needed: str = Field(
        description="질문이 불명확할 때 사용자에게 물어볼 추가 확인 질문입니다."
    )
