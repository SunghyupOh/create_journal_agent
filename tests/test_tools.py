"""tools.DiffTool 테스트 — git 은 monkeypatch 로 대체한다."""

import subprocess

import pytest

from journal.tools import TOOL_SCHEMA, DiffTool


def fake_git(returns: str):
    """_git 대역. 호출 인자를 기록하고 정해진 문자열을 돌려준다."""
    calls: list[tuple] = []

    def _fake(repo, *args):
        calls.append((repo, args))
        return returns

    return _fake, calls


# --- 정상 경로 ---------------------------------------------------------------


def test_returns_diff(monkeypatch):
    fake, calls = fake_git("diff --git a/x b/x\n+added line")
    monkeypatch.setattr("journal.tools._git", fake)

    tool = DiffTool({"abc1234": "/home/u/repo"})
    out = tool.get_full_diff("abc1234")

    assert "+added line" in out
    assert calls[0][0] == "/home/u/repo"
    assert "abc1234" in calls[0][1]


def test_file_path_narrows_query(monkeypatch):
    fake, calls = fake_git("diff --git a/x b/x")
    monkeypatch.setattr("journal.tools._git", fake)

    DiffTool({"abc1234": "/r"}).get_full_diff("abc1234", file_path="src/x.py")

    # `--` 뒤에 파일 경로가 붙어야 한다
    args = calls[0][1]
    assert args[-2:] == ("--", "src/x.py")


# --- 실패는 예외가 아니라 문자열 ----------------------------------------------


def test_unknown_hash_returns_error_string(monkeypatch):
    fake, calls = fake_git("")
    monkeypatch.setattr("journal.tools._git", fake)

    out = DiffTool({"abc1234": "/r"}).get_full_diff("없는해시")
    assert out.startswith("오류")
    assert calls == []  # git 을 부르지도 않는다


def test_git_failure_returns_masked_error(monkeypatch):
    def boom(repo, *args):
        raise subprocess.CalledProcessError(
            128, ["git"], stderr="fatal: https://user:ghp_abcdefghijklmnopqrstuvwxyz123456@github.com/x"
        )

    monkeypatch.setattr("journal.tools._git", boom)

    out = DiffTool({"abc1234": "/r"}).get_full_diff("abc1234")
    assert out.startswith("오류")
    assert "ghp_" not in out  # 토큰이 마스킹돼야 한다


def test_empty_patch_hint(monkeypatch):
    fake, _ = fake_git("   \n")
    monkeypatch.setattr("journal.tools._git", fake)

    out = DiffTool({"abc1234": "/r"}).get_full_diff("abc1234", file_path="오타.py")
    assert "결과 없음" in out


# --- 상한 --------------------------------------------------------------------


def test_call_limit(monkeypatch):
    fake, calls = fake_git("diff")
    monkeypatch.setattr("journal.tools._git", fake)

    tool = DiffTool({"abc1234": "/r"}, max_calls=2)
    tool.get_full_diff("abc1234")
    tool.get_full_diff("abc1234")
    out = tool.get_full_diff("abc1234")

    assert "한도" in out
    assert len(calls) == 2  # 3번째는 git 을 부르지 않는다


def test_line_cap(monkeypatch):
    fake, _ = fake_git("\n".join(f"line{i}" for i in range(100)))
    monkeypatch.setattr("journal.tools._git", fake)

    out = DiffTool({"abc1234": "/r"}, max_lines=10).get_full_diff("abc1234")
    assert "line9" in out
    assert "line10" not in out
    assert "생략됨" in out


# --- 출력 마스킹 --------------------------------------------------------------


def test_diff_output_is_masked(monkeypatch):
    fake, _ = fake_git("+ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop")
    monkeypatch.setattr("journal.tools._git", fake)

    out = DiffTool({"abc1234": "/r"}).get_full_diff("abc1234")
    assert "sk-ant-" not in out


# --- 터미널 로그 --------------------------------------------------------------


def test_call_is_logged_to_stderr(monkeypatch, capsys):
    fake, _ = fake_git("diff --git a/x b/x")
    monkeypatch.setattr("journal.tools._git", fake)

    DiffTool({"abc1234": "/r"}).get_full_diff("abc1234", file_path="src/x.py")

    err = capsys.readouterr().err
    assert "[도구]" in err
    assert "abc1234 src/x.py" in err
    assert "줄 반환" in err


# --- 스키마 핀 ---------------------------------------------------------------


def test_schema_shape():
    """API 에 넘어가는 계약 — 이름·필수 입력이 바뀌면 프롬프트도 같이 봐야 한다."""
    assert TOOL_SCHEMA["name"] == "get_full_diff"
    assert TOOL_SCHEMA["input_schema"]["required"] == ["commit_hash"]
