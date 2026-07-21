"""LLM 출력 스키마 (§4, D23).

`messages.parse(output_format=JournalSummary)` 에 그대로 넘긴다. 스키마 준수를 API가
보장하므로 파싱 코드가 없다.

structured outputs 제약: 재귀 스키마·수치 제약·문자열 길이 제약 미지원 — 여기 모델은
전부 평평한 리스트라 해당 없다. 제약을 추가하고 싶어질 때 이 주석을 먼저 볼 것.
"""

from __future__ import annotations

from pydantic import BaseModel

#: 스키마 구조가 바뀌면 올린다. 입력 지문(10+ 단계)에 들어가 재요약을 유발한다.
OUTPUT_SCHEMA_VERSION = "1"


class WorkItem(BaseModel):
    desc: str
    project: str
    sources: list[str]  # "commit:a1b2c3" | "session:세션ID"


class TroubleItem(BaseModel):
    problem: str
    cause: str
    solution: str
    project: str
    sources: list[str]


class PendingItem(BaseModel):
    desc: str
    sources: list[str]


class JournalSummary(BaseModel):
    work: list[WorkItem]
    troubleshooting: list[TroubleItem]
    pending: list[PendingItem]
