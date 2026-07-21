"""§7 경로 정규화 테스트 케이스."""

import os

import pytest

from journal.paths import (
    dedupe_repo_roots,
    find_repo_root,
    is_within,
    realpath,
    to_posix,
    to_repo_relative,
)

# --- to_posix: UNC → POSIX -------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 실측 값 (§7-A)
        (r"\\wsl.localhost\Ubuntu\home\oh\ai_mentor\backend", "/home/oh/ai_mentor/backend"),
        (
            r"\\wsl.localhost\Ubuntu\home\ohsunghyup\ai_mentor\backend\llm\__init__.py",
            "/home/ohsunghyup/ai_mentor/backend/llm/__init__.py",
        ),
        # 구형 표기
        (r"\\wsl$\Ubuntu\home\oh\proj", "/home/oh/proj"),
        # 대소문자 무관
        (r"\\WSL.LOCALHOST\Ubuntu\home\oh\proj", "/home/oh/proj"),
        # 다른 배포판도 통과 (접두사를 config에 고정하지 않은 이유)
        (r"\\wsl.localhost\Debian\home\oh\proj", "/home/oh/proj"),
        # 이미 POSIX면 그대로
        ("/home/oh/ai_mentor", "/home/oh/ai_mentor"),
        # 후행 슬래시 제거 — 비교 시 어긋나는 원인
        ("/home/oh/ai_mentor/", "/home/oh/ai_mentor"),
        (r"\\wsl.localhost\Ubuntu\home\oh\proj\\", "/home/oh/proj"),
        # 루트는 유지
        ("/", "/"),
        # None/빈 문자열 통과
        (None, None),
        ("", ""),
    ],
)
def test_to_posix(raw, expected):
    assert to_posix(raw) == expected


def test_to_posix_keeps_windows_drive():
    """WSL 밖 경로는 드라이브 표기를 유지한다 — 저장소 루트와 우연히 겹치지 않게."""
    assert to_posix(r"C:\Users\oh\proj") == "C:/Users/oh/proj"


# --- is_within: 경로 경계 --------------------------------------------------


def test_is_within_uses_path_boundary_not_string_prefix():
    """문자열 접두사로 비교하면 _backup 이 잘못 매칭된다."""
    assert is_within("/home/oh/ai_mentor/backend", "/home/oh/ai_mentor")
    assert not is_within("/home/oh/ai_mentor_backup", "/home/oh/ai_mentor")
    assert not is_within("/home/oh/ai_mentor_backup/x.py", "/home/oh/ai_mentor")


def test_is_within_self_and_parent():
    assert is_within("/home/oh/ai_mentor", "/home/oh/ai_mentor")
    assert not is_within("/home/oh", "/home/oh/ai_mentor")


# --- to_repo_relative ------------------------------------------------------


def test_to_repo_relative_inside():
    assert (
        to_repo_relative("/home/oh/ai_mentor/backend/llm/client.py", "/home/oh/ai_mentor")
        == "backend/llm/client.py"
    )


def test_to_repo_relative_outside_keeps_absolute():
    """저장소 밖 경로는 상대화하지 않고 절대경로 유지 (§7 케이스 3)."""
    assert to_repo_relative("/home/oh/other/x.py", "/home/oh/ai_mentor") == "/home/oh/other/x.py"
    assert to_repo_relative("C:/Users/oh/x.py", "/home/oh/ai_mentor") == "C:/Users/oh/x.py"


def test_to_repo_relative_root_itself():
    assert to_repo_relative("/home/oh/ai_mentor", "/home/oh/ai_mentor") == ""


def test_repo_relative_matches_git_files_format():
    """git이 주는 files 와 같은 모양이어야 겹침 신호가 성립한다 (D13)."""
    session_path = to_posix(
        r"\\wsl.localhost\Ubuntu\home\ohsunghyup\ai_mentor\backend\llm\client.py"
    )
    git_file = "backend/llm/client.py"
    assert to_repo_relative(session_path, "/home/ohsunghyup/ai_mentor") == git_file


# --- find_repo_root --------------------------------------------------------


def test_find_repo_root_nearest_ancestor_wins():
    roots = ["/home/oh/ai_mentor", "/home/oh/ai_mentor/vendor/lib"]
    assert (
        find_repo_root("/home/oh/ai_mentor/vendor/lib/a.py", roots)
        == "/home/oh/ai_mentor/vendor/lib"
    )
    assert find_repo_root("/home/oh/ai_mentor/backend/a.py", roots) == "/home/oh/ai_mentor"


def test_find_repo_root_none_is_etc_bucket():
    """어느 저장소에도 안 속하면 None → F2-2 의 "기타" 버킷."""
    roots = ["/home/oh/ai_mentor"]
    assert find_repo_root("/home/oh/scratch", roots) is None
    assert find_repo_root("C:/Users/oh/proj", roots) is None


def test_find_repo_root_empty_roots():
    assert find_repo_root("/home/oh/x", []) is None


# --- realpath / dedupe (파일 시스템 접근) ----------------------------------


def test_realpath_resolves_symlink(tmp_path):
    """심볼릭 링크가 걸린 저장소 루트가 동일 판정되는가 (§7 케이스 4)."""
    real = tmp_path / "repo"
    real.mkdir()
    link = tmp_path / "repo-link"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크 생성 불가")

    assert realpath(str(link)) == realpath(str(real))


def test_dedupe_repo_roots_preserves_order(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    out = dedupe_repo_roots([str(b), str(a), str(b)])
    assert out == [realpath(str(b)), realpath(str(a))]


def test_dedupe_repo_roots_collapses_symlink(tmp_path):
    real = tmp_path / "repo"
    real.mkdir()
    link = tmp_path / "repo-link"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크 생성 불가")

    assert len(dedupe_repo_roots([str(real), str(link)])) == 1
