import json
from typing import List
from pydantic import BaseModel, Field, field_validator


def _model_dict_to_text(value: dict) -> str:
    """Model-authored sub-object → readable text. Keys are kept — they may label the
    values (e.g. {"신청": "18학점", "졸업요건": "21학점"})."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)

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
    required_conditions: List[str] = Field(default_factory=list, description="답이 개인 조건에 따라 달라지는 질문에서, 정확한 답을 위해 필요하지만 질문에 명시되지 않은 조건 이름 목록 (예: '휴학 유형', '학번'). 규정 조회형(일반 사실) 질문이거나 조건이 이미 명시돼 있으면 반드시 빈 목록.")

    # --- Type tolerance -----------------------------------------------------
    # These fields are populated from MODEL-AUTHORED tool-call JSON, where "empty" and
    # "number" arrive in whatever shape the model chose. Pydantic rejects the whole object
    # on a single mismatch, so ONE stray field discards every other slot and the question
    # silently degrades to no-slots. Measured live (2026-07-27, qwen3.5:9b, "이번 학기
    # 18학점 신청했는데 …"): the model returned extra="" and the ValidationError threw away
    # the correctly-extracted credits/semester too. Coerce the known mismatches instead.

    @field_validator("admission_year", "grade", "semester", "status", "major", "credits",
                     "leave_type", mode="before")
    @classmethod
    def _coerce_scalar(cls, value):
        """null → "", number/bool → its text, dict → its JSON, list → comma-joined text."""
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            return _model_dict_to_text(value) if value else ""
        if isinstance(value, (list, tuple)):
            return ", ".join(
                str(v).strip() for v in value if v is not None and str(v).strip())
        return value

    @field_validator("extra", "required_conditions", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        """null/"" (the model's way of saying "none") → [], bare string/dict → one-item list."""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, dict):
            return [_model_dict_to_text(value)] if value else []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if v is not None and str(v).strip()]
        return value


class SelfCheckVerdict(BaseModel):
    """답변 전 자가검사 판정 (#176 = #145 처방 4).

    최종 답변 초안을 근거(검색된 답변들)와 대조한 JUDGE 출력. ok=True면 초안이
    바이트 그대로 유지되므로, 확실하지 않을 때만 False로 판정해야 한다 —
    과잉 지적은 불필요한 재작성(정답 열화 위험)으로 직결된다.
    """
    ok: bool = Field(description="초안의 모든 단정이 근거에 있고, 조건에 따라 달라지는 규정을 무조건 단정하지 않았으면 True.")
    unsupported_claims: List[str] = Field(default_factory=list, description="근거에 없는데 단정한 주장 목록 (초안 원문 표현 그대로). ok=True면 빈 목록.")
    missing_conditions: List[str] = Field(default_factory=list, description="답이 달라지게 만드는데 질문에 없는 조건 이름 목록 (예: '휴학 유형'). ok=True면 빈 목록.")

    # UserSlots와 같은 이유(41c8f2a 실측: 필드 하나의 타입 불일치가 객체 전체를 폐기)로
    # 모델-작성 JSON의 흔한 형태 불일치를 흡수한다. 판정 폐기 = 레버 조용한 무효화.
    @field_validator("unsupported_claims", "missing_conditions", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, dict):
            return [_model_dict_to_text(value)] if value else []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if v is not None and str(v).strip()]
        return value
