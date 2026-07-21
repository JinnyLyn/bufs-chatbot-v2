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


class UserSlots(BaseModel):
    """사용자 질문에 '명시적으로' 적힌 개인 상황 조건(슬롯) — issue #145 처방 1.

    시나리오형 질문("2024학번인데 휴학 연장 되나요?")에서 답을 좌우하는 조건을
    구조화해 생성 단계에 넘긴다. 질문에 없는 필드는 반드시 빈 값으로 남긴다
    (추론 금지 — 잘못 채운 슬롯은 잘못된 규정 적용으로 직결).
    """
    admission_year: str = Field(default="", description="학번/입학년도. 질문에 명시된 경우만 원문 표현 그대로 (예: '2024학번', '17학번'). 없으면 빈 문자열.")
    grade: str = Field(default="", description="학년 (예: '3학년', '4학년 마지막 학기'). 명시된 경우만.")
    semester: str = Field(default="", description="질문이 가리키는 대상 학기 (예: '이번 학기', '계절학기'). 명시된 경우만.")
    status: str = Field(default="", description="학적 신분 (예: '재학 중', '휴학 중', '복학 예정', '졸업예정자', '편입생'). 명시된 경우만.")
    major: str = Field(default="", description="전공/학과와 복수전공·부전공·전과 여부. 명시된 경우만.")
    credits: str = Field(default="", description="이수학점·평점 등 수치 조건 (예: '18학점 신청', '평점 4.0'). 명시된 경우만.")
    leave_type: str = Field(default="", description="휴학 유형 (예: '일반휴학', '병역휴학', '질병휴학'). 명시된 경우만.")
    extra: List[str] = Field(default_factory=list, description="그 외 답변에 영향을 주는 명시적 개인 조건 (예: '등록금 일부만 납부', '수강신청 기간을 놓침'). 질문의 주제 자체는 넣지 않는다.")
