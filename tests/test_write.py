"""render 순수 함수 테스트 — DB·파일 없이."""

from datetime import date

from journal.schema import JournalSummary, PendingItem, TroubleItem, WorkItem
from journal.write import render

DAY = date(2026, 7, 14)

FULL = JournalSummary(
    work=[
        WorkItem(
            desc="커리큘럼 목록을 그리드 카드로 재설계",
            project="dtartup-client",
            sources=["commit:b797bc5", "session:72b87047-4c78-487c-bc34-6ac5e4600f05"],
        )
    ],
    troubleshooting=[
        TroubleItem(
            problem="지연로딩 쿼리가 실행되지 않음",
            cause="1차 캐시가 동일 인스턴스를 반환",
            solution="flush/clear 후 재조회",
            project="기타",
            sources=["session:db860e75-607b-4428-bba9-0a45974f50a1"],
        )
    ],
    pending=[PendingItem(desc="worktree 사용 여부 확인", sources=[])],
)

EMPTY = JournalSummary(work=[], troubleshooting=[], pending=[])


def test_full_render_structure():
    md = render(DAY, FULL)
    assert md.startswith("# 2026-07-14 개발 일지")
    assert "## 작업 내용" in md and "## 트러블슈팅" in md and "## 미해결" in md
    assert "**[dtartup-client]** 커리큘럼 목록을 그리드 카드로 재설계" in md
    assert "- **원인**: 1차 캐시가 동일 인스턴스를 반환" in md
    assert "- worktree 사용 여부 확인" in md


def test_session_id_shortened_commit_kept():
    md = render(DAY, FULL)
    assert "session:72b87047" in md
    assert "72b87047-4c78" not in md  # 전체 UUID는 안 나온다
    assert "commit:b797bc5" in md


def test_empty_sections_say_none():
    """'없음' 표기는 LLM이 아니라 코드 몫이다 (핵심 설계)."""
    md = render(DAY, EMPTY)
    assert md.count("- 없음") == 3


def test_no_sources_no_source_line():
    md = render(DAY, FULL)
    pending_part = md.split("## 미해결")[1]
    assert "출처" not in pending_part  # pending 항목의 sources가 비어 있으므로


def test_roundtrip_from_json():
    """DB jsonb 에서 나온 dict 로도 렌더링된다 (write_day 경로)."""
    restored = JournalSummary.model_validate(FULL.model_dump())
    assert render(DAY, restored) == render(DAY, FULL)
