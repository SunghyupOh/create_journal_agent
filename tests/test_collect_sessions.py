"""§7-A 추출 규칙 테스트 — 실측으로 확정한 함정들을 고정한다."""

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from journal.collect_sessions import (
    COLUMNS,
    discover_session_files,
    extract_file,
    to_row,
)

KST = ZoneInfo("Asia/Seoul")
TS = "2026-07-19T05:00:00.000Z"  # UTC 05시 = KST 14시 → 7/19


def _entry(**kw) -> str:
    base = {"timestamp": TS, "cwd": r"\\wsl.localhost\Ubuntu\home\oh\proj"}
    base.update(kw)
    return json.dumps(base)


def _human(text, **kw) -> str:
    return _entry(type="user", origin={"kind": "human"}, message={"content": text}, **kw)


def _assistant(blocks, **kw) -> str:
    return _entry(type="assistant", message={"content": blocks}, **kw)


def _write(tmp_path, lines) -> Path:
    p = tmp_path / "abc-session.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --- §7-A 함정 1: 도구 출력이 user 로 위장 ----------------------------------


def test_tool_result_disguised_as_user_is_dropped(tmp_path):
    p = _write(
        tmp_path,
        [
            _human("진짜 사람 메시지"),
            # origin 없음 — 도구 출력 (실측: user 79개 중 63개가 이것)
            _entry(type="user", message={"content": [{"type": "tool_result", "content": "..."}]}),
            _entry(type="user", origin={"kind": "tool"}, message={"content": "도구발 메시지"}),
        ],
    )
    groups = extract_file(p, KST)
    texts = groups[date(2026, 7, 19)].texts
    assert texts == ["[사용자] 진짜 사람 메시지"]


# --- §7-A 함정 2: isSidechain None 다수 -------------------------------------


def test_sidechain_true_dropped_none_kept(tmp_path):
    p = _write(
        tmp_path,
        [
            _human("메인 대화", isSidechain=None),
            _human("사이드체인", isSidechain=True),
            _human("플래그 없음"),
        ],
    )
    texts = extract_file(p, KST)[date(2026, 7, 19)].texts
    assert texts == ["[사용자] 메인 대화", "[사용자] 플래그 없음"]


# --- assistant 블록 ----------------------------------------------------------


def test_assistant_text_kept_thinking_dropped(tmp_path):
    p = _write(
        tmp_path,
        [
            _assistant(
                [
                    {"type": "thinking", "thinking": "생각중..."},
                    {"type": "text", "text": "응답 텍스트"},
                    {"type": "text", "text": "   "},  # 공백뿐 → 버림
                ]
            )
        ],
    )
    texts = extract_file(p, KST)[date(2026, 7, 19)].texts
    assert texts == ["[클로드] 응답 텍스트"]


def test_edit_write_paths_extracted_content_dropped(tmp_path):
    p = _write(
        tmp_path,
        [
            _assistant(
                [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": r"\\wsl.localhost\Ubuntu\home\oh\proj\a.py",
                            "new_string": "SECRET=123",  # content 는 버려진다 (D11)
                        },
                    },
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": r"\\wsl.localhost\Ubuntu\home\oh\proj\b.py"},
                    },
                ]
            )
        ],
    )
    g = extract_file(p, KST)[date(2026, 7, 19)]
    assert g.files == ["/home/oh/proj/a.py", "/home/oh/proj/b.py"]  # UNC → POSIX
    assert g.texts == []  # Bash 는 무시, tool_use 는 텍스트가 아니다


# --- 날짜 분할 ---------------------------------------------------------------


def test_utc_to_kst_date_split(tmp_path):
    """UTC 15시 = KST 다음날 0시 — 날짜 경계가 KST 기준으로 갈려야 한다."""
    p = _write(
        tmp_path,
        [
            _human("7/19 저녁", timestamp="2026-07-19T10:00:00.000Z"),  # KST 19시
            _human("7/20 자정", timestamp="2026-07-19T15:00:00.000Z"),  # KST 다음날 0시
        ],
    )
    groups = extract_file(p, KST)
    assert groups[date(2026, 7, 19)].texts == ["[사용자] 7/19 저녁"]
    assert groups[date(2026, 7, 20)].texts == ["[사용자] 7/20 자정"]


def test_entry_without_timestamp_skipped(tmp_path):
    line = json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": "x"}})
    p = _write(tmp_path, [line])
    assert extract_file(p, KST) == {}


def test_broken_json_line_skipped(tmp_path):
    """기록 중인 파일은 마지막 줄이 잘려 있을 수 있다."""
    p = _write(tmp_path, [_human("정상"), '{"type": "user", "mess'])
    assert extract_file(p, KST)[date(2026, 7, 19)].texts == ["[사용자] 정상"]


# --- to_row ------------------------------------------------------------------


def test_to_row_mode_cwd_and_mixed_flag(tmp_path):
    p = _write(
        tmp_path,
        [
            _human("a", cwd=r"\\wsl.localhost\Ubuntu\home\oh\proj"),
            _human("b", cwd=r"\\wsl.localhost\Ubuntu\home\oh\proj"),
            _human("c", cwd=r"\\wsl.localhost\Ubuntu\home\oh\other"),
        ],
    )
    g = extract_file(p, KST)[date(2026, 7, 19)]
    row = to_row("sess1", date(2026, 7, 19), g)
    assert row[0] == "sess1"
    assert row[2] == "/home/oh/proj"  # 최빈 cwd (D19)
    assert row[3] is True  # cwd_mixed
    assert len(row) == len(COLUMNS)


def test_to_row_masks_text(tmp_path):
    p = _write(tmp_path, [_human("키는 sk-ant-api03-AbCdEf0123456789xyz 야")])
    row = to_row("s", date(2026, 7, 19), extract_file(p, KST)[date(2026, 7, 19)])
    assert "sk-ant-" not in row[5]
    assert "[MASKED:anthropic]" in row[5]


def test_to_row_dedupes_files_keeps_order(tmp_path):
    p = _write(
        tmp_path,
        [
            _assistant(
                [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/b.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/b.py"}},
                ]
            )
        ],
    )
    row = to_row("s", date(2026, 7, 19), extract_file(p, KST)[date(2026, 7, 19)])
    assert row[6] == "/b.py\n/a.py"


# --- 파일 탐지 (F1-2) --------------------------------------------------------


def test_discover_excludes_subagents(tmp_path):
    main = tmp_path / "proj" / "sess.jsonl"
    sub = tmp_path / "proj" / "sess" / "subagents" / "agent-1.jsonl"
    main.parent.mkdir(parents=True)
    sub.parent.mkdir(parents=True)
    main.write_text("{}")
    sub.write_text("{}")

    found = discover_session_files(str(tmp_path), date(2020, 1, 1))
    assert found == [main]


def test_discover_none_floor_returns_empty(tmp_path):
    (tmp_path / "s.jsonl").write_text("{}")
    assert discover_session_files(str(tmp_path), None) == []


def test_discover_mtime_filter(tmp_path):
    import os

    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (0, 0))  # 1970년

    found = discover_session_files(str(tmp_path), date(2026, 1, 1))
    assert found == [new]
