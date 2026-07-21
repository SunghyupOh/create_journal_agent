"""클로드 코드 세션 수집 (F1-2 탐지, F1-3 추출).

Sessions_extract 테이블에 LLM에게 넘겨줄 파싱 데이터가 담긴다.
sessions 테이블에는 그 세션 파일 메타 데이터가 담김.
collect_git 과 같은 방식으로 저장할 데이터를 변수에 담은 후 db.replace_for_date 로 하루 단위로 행생성
collect_git -> git log에서 파싱, collect_sessions -> JSONL에서 파싱

`SESSIONS_DIR` 아래 JSONL을 재파싱해 §7-A 확정 규칙대로 뽑는다:

    ⓐ 사람이 친 메시지 + 클로드 텍스트 응답      → text
    ⓑ Edit/Write 의 **수정 파일 경로만** (D11)   → edited_files
    ⓒ 엔트리의 cwd (최빈값)                      → project_dir
    ⓓ gitBranch                                  → git_branch

메시지 타임스탬프(UTC)를 Asia/Seoul 날짜로 바꿔 **(session_id, extract_date) 단위로
그룹핑**하고, 날짜별 REPLACE 한다. 매번 전체 재파싱 — 결정적 재생성 (D5).

§7-A 의 두 함정이 이 모듈의 존재 이유다:
- 도구 출력이 user 엔트리로 위장한다 (실측: user 79개 중 63개). `origin.kind == "human"` 으로 거른다.
- `isSidechain` 이 없는(None) 엔트리가 많다. `is True` 로 비교한다.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from journal import config, db
from journal.mask import mask
from journal.paths import to_posix

#: `session_extracts` 컬럼 순서. 삽입 튜플과 반드시 같은 순서.
COLUMNS = [
    "session_id",
    "extract_date",
    "project_dir",
    "cwd_mixed",
    "git_branch",
    "text",
    "edited_files",
]


@dataclass
class DayGroup:
    """한 세션의 하루치 추출 결과 (가공 전)."""

    texts: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    cwds: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)


# --- 파일 탐지 (F1-2) -------------------------------------------------------


def discover_session_files(root: str, floor: date | None) -> list[Path]:
    """mtime이 `floor` 이후인 JSONL 파일. `subagents/` 하위는 통째로 제외.

    이 필터는 순수 성능 최적화다 — 정합성은 날짜별 재파싱(F1-3)이 보장한다.
    `floor` 가 None(후보 없음)이면 빈 리스트.
    """
    if floor is None:
        return []

    base = Path(root)
    if not base.is_dir():
        return []

    cutoff = datetime(floor.year, floor.month, floor.day, tzinfo=timezone.utc).timestamp()

    out: list[Path] = []
    for p in base.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue  # 서브에이전트 transcript는 별도 파일로도 존재한다 (§8)
        if p.stat().st_mtime >= cutoff:
            out.append(p)
    return sorted(out)


# --- 엔트리 추출 (F1-3, §7-A) -----------------------------------------------


def _text_of(content) -> str:
    """user 엔트리의 content — str일 수도, 블록 리스트일 수도 있다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _entry_date(entry: dict, tz: ZoneInfo) -> date | None:
    """`"2026-06-23T13:47:29.439Z"` (UTC) → Asia/Seoul 날짜."""
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(tz).date()
    except ValueError:
        return None


def extract_file(path: Path, tz: ZoneInfo) -> dict[date, DayGroup]:
    """세션 파일 하나 → 날짜별 DayGroup. §7-A 확정 규칙 그대로.

    깨진 JSON 줄은 건너뛴다 — 기록 중인 파일을 읽으면 마지막 줄이 잘려 있을 수 있다.
    """
    groups: dict[date, DayGroup] = defaultdict(DayGroup)

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("isSidechain") is True:  # None 이 다수라 `is True` (§7-A 함정 2)
                continue

            day = _entry_date(entry, tz)
            if day is None:
                continue
            t = entry.get("type")

            # 함정 1: 도구 출력이 user 로 위장한다. origin.kind == "human" 만 사람 메시지.
            if t == "user" and entry.get("origin", {}).get("kind") == "human":
                text = _text_of(entry.get("message", {}).get("content"))
                if text.strip():
                    g = groups[day]
                    g.texts.append(f"[사용자] {text.strip()}")
                    _note_context(g, entry)

            elif t == "assistant":
                content = entry.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                g = None
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():  # thinking 제외
                        g = g or groups[day]
                        g.texts.append(f"[클로드] {b['text'].strip()}")
                    elif b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write"):
                        fp = to_posix(b.get("input", {}).get("file_path"))
                        if fp:
                            g = g or groups[day]
                            g.files.append(fp)  # content 는 버린다 (D11)
                if g is not None:
                    _note_context(g, entry)

    return dict(groups)


def _note_context(g: DayGroup, entry: dict) -> None:
    """추출된 엔트리의 cwd·gitBranch 를 그룹에 적립 — 나중에 최빈값을 뽑는다."""
    cwd = to_posix(entry.get("cwd"))
    if cwd:
        g.cwds.append(cwd)
    branch = entry.get("gitBranch")
    if branch:
        g.branches.append(branch)


# --- 행 변환 ----------------------------------------------------------------


def _mode(values: list[str]) -> str | None:
    """최빈값. 비면 None."""
    return Counter(values).most_common(1)[0][0] if values else None


def _dedupe(values: list[str]) -> list[str]:
    """순서 유지 중복 제거."""
    return list(dict.fromkeys(values))


def to_row(session_id: str, day: date, g: DayGroup) -> tuple:
    """DayGroup → 삽입 튜플. COLUMNS 순서와 일치해야 한다.

    project_dir 는 **최빈 cwd** — 세션 하나에 cwd 가 2종 이상 관측된 게 실측 사실이라(D19)
    다수결로 정하고 `cwd_mixed` 로 흔적을 남긴다.
    """
    distinct_cwds = set(g.cwds)
    return (
        session_id,
        day,
        _mode(g.cwds),
        len(distinct_cwds) > 1,
        _mode(g.branches),
        mask("\n\n".join(g.texts)) or "",
        "\n".join(_dedupe(g.files)),
    )


# --- 수집 (공개 함수) --------------------------------------------------------


def collect(conn: psycopg.Connection, days: list[date]) -> dict[date, int]:
    """`days` 의 세션 추출을 수집해 날짜별 REPLACE. `{날짜: 행 수}` 반환.

    파일 하나가 깨져도 나머지는 계속한다 (collect_git 과 같은 원칙).
    """
    from journal.dates import session_mtime_floor  # 순환 import 없음 — dates 는 순수 모듈

    tz = ZoneInfo(config.TIMEZONE)
    floor = session_mtime_floor(days, config.SESSION_MTIME_MARGIN_DAYS)
    files = discover_session_files(config.SESSIONS_DIR, floor)
    wanted = set(days)

    by_date: dict[date, list[tuple]] = defaultdict(list)

    for path in files:
        session_id = path.stem
        try:
            groups = extract_file(path, tz)
        except OSError as e:
            db.log_run(conn, "collect_sessions", "warn", detail=f"{path}: {e}")
            continue

        touched = False
        for day, g in groups.items():
            if day in wanted and (g.texts or g.files):
                by_date[day].append(to_row(session_id, day, g))
                touched = True

        if touched:
            _upsert_session_meta(conn, session_id, path)

    counts: dict[date, int] = {}
    for day in days:
        counts[day] = db.replace_for_date(
            conn, "session_extracts", "extract_date", day, COLUMNS, by_date.get(day, [])
        )
    return counts


def _upsert_session_meta(conn: psycopg.Connection, session_id: str, path: Path) -> None:
    """`sessions` 메타 갱신. 진단용이라 실패해도 수집을 막지 않을 가치는 없다 — 그냥 upsert."""
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into sessions (session_id, transcript_path, last_seen_mtime)
                values (%s, %s, %s)
                on conflict (session_id)
                do update set transcript_path = excluded.transcript_path,
                              last_seen_mtime = excluded.last_seen_mtime
                """,
                (session_id, str(path), mtime),
            )
