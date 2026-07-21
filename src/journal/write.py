"""일지 렌더링 (F3-1) — JSON → 마크다운.

판단은 LLM, 포맷팅은 코드 (D 핵심 설계). LLM의 JSON은 중간 표현이고 사람이 읽는 건
여기서 만드는 마크다운이다. 섹션 구성·"없음" 표기·출처 표기가 전부 코드에 있으므로
포맷을 바꿔도 LLM 재호출 없이 재렌더링만 하면 된다.

일지 마크다운은 LLM 출력(journals.summary_json)에서만 렌더링한다 — DB 원본 텍스트를
직접 넣지 않는다 (F2-6 불변 조건, 심층 방어).

git 커밋은 10+ 단계에서 붙인다.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import psycopg

from journal import config, db
from journal.schema import JournalSummary


def _fmt_source(src: str) -> str:
    """출처 표기. 세션 ID(UUID)는 8자로 줄인다 — 사람이 읽는 문서라서.

    >>> _fmt_source("commit:a1b2c3")
    'commit:a1b2c3'
    >>> _fmt_source("session:72b87047-4c78-487c-bc34-6ac5e4600f05")
    'session:72b87047'
    """
    if src.startswith("session:"):
        return "session:" + src.removeprefix("session:")[:8]
    return src


def _sources_line(sources: list[str]) -> str:
    return f"  - 출처: {', '.join(_fmt_source(s) for s in sources)}" if sources else ""


def render(day: date, summary: JournalSummary) -> str:
    """마크다운 본문. 순수 함수 — 파일·DB 안 만진다."""
    lines: list[str] = [f"# {day.isoformat()} 개발 일지", ""]

    lines.append("## 작업 내용")
    if summary.work:
        for w in summary.work:
            lines.append(f"- **[{w.project}]** {w.desc}")
            if s := _sources_line(w.sources):
                lines.append(s)
    else:
        lines.append("- 없음")
    lines.append("")

    lines.append("## 트러블슈팅")
    if summary.troubleshooting:
        for t in summary.troubleshooting:
            lines.append(f"### [{t.project}] {t.problem}")
            lines.append(f"- **원인**: {t.cause}")
            lines.append(f"- **해결**: {t.solution}")
            if s := _sources_line(t.sources):
                lines.append(s)
            lines.append("")
    else:
        lines.append("- 없음")
        lines.append("")

    lines.append("## 미해결")
    if summary.pending:
        for p in summary.pending:
            lines.append(f"- {p.desc}")
            if s := _sources_line(p.sources):
                lines.append(s)
    else:
        lines.append("- 없음")
    lines.append("")

    return "\n".join(lines)


def write_day(conn: psycopg.Connection, day: date) -> str | None:
    """journals 의 summary_json 을 읽어 마크다운 파일로 쓴다. 경로 반환.

    skipped/failed(summary_json 없음)면 파일을 만들지 않고 None (F2-4).
    같은 날짜 재실행은 파일을 통째로 덮어쓴다 — 결정적 재생성 (D5).
    """
    with conn.cursor() as cur:
        cur.execute("select summary_json from journals where journal_date = %s", (day,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None

    summary = JournalSummary.model_validate(row[0])

    out_dir = Path(os.path.expanduser(config.JOURNAL_DIR))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}.md"
    path.write_text(render(day, summary), encoding="utf-8")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "update journals set markdown_path = %s where journal_date = %s",
                (str(path), day),
            )
    db.log_run(conn, "write", "ok", journal_date=day, detail=str(path))
    return str(path)
