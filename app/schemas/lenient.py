# app/schemas/lenient.py
"""LLM 출력을 받는 모델의 공통 베이스.

구조화 출력은 `method="function_calling"` 으로 받는다. 엄격 모드(json_schema)가
자유형 dict 를 거부해서인데, 대신 모델이 값을 모를 때 필드를 생략하지 않고
`null` 을 넣어 보낸다. 기본값이 있어도 명시적 null 은 검증에서 걸리므로
여기서 걷어낸다.

리스트 안에 빈 문자열 같은 이물질이 섞여 오는 경우도 있어 같이 정리한다.
"""

from typing import Any

from pydantic import BaseModel, model_validator


class LenientModel(BaseModel):
    """null 과 이물질을 걷어내고 기본값으로 떨어지게 한다."""

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                continue  # 기본값이 대신 들어간다
            if isinstance(value, list):
                value = [v for v in value if v not in ("", None)]
            cleaned[key] = value
        return cleaned
