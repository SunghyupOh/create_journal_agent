"""collect_git.py 중 DB 없이 검증 가능한 부분 — 저장소 발견, diff 상한, 컬럼 순서."""

import subprocess

import pytest

from journal.collect_git import COLUMNS, _split_by_file, discover_repos, truncate_diff


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


# --- 저장소 발견 ------------------------------------------------------------


def test_discover_finds_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git_init(repo)
    assert discover_repos([str(tmp_path)]) == [str(repo)]


def test_discover_skips_hidden_dirs(tmp_path):
    """~/.nvm, ~/.oh-my-zsh 안의 .git 이 잡히면 남의 커밋이 일지에 섞인다."""
    hidden = tmp_path / ".nvm" / "lib"
    hidden.mkdir(parents=True)
    _git_init(hidden)
    assert discover_repos([str(tmp_path)]) == []


def test_discover_skips_excluded_dirs(tmp_path):
    nested = tmp_path / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    _git_init(nested)
    assert discover_repos([str(tmp_path)]) == []


def test_discover_dedupes_symlinked_root(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git_init(repo)
    link = tmp_path / "proj-link"
    try:
        link.symlink_to(repo)
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크 생성 불가")

    assert len(discover_repos([str(tmp_path)])) == 1


def test_discover_ignores_missing_root():
    assert discover_repos(["/존재하지/않는/경로"]) == []


# --- diff 상한 --------------------------------------------------------------

TWO_FILES = "\n".join(
    ["diff --git a/x.py b/x.py", "+1", "+2", "+3", "diff --git a/y.py b/y.py", "+a", "+b"]
)


def test_split_by_file():
    assert len(_split_by_file(TWO_FILES)) == 2


def test_split_by_file_single():
    assert len(_split_by_file("diff --git a/x.py b/x.py\n+1")) == 1


def test_truncate_per_file_marks_and_notes():
    out, cut = truncate_diff(TWO_FILES, max_per_file=2, max_per_commit=100)
    assert cut is True
    assert "생략됨" in out
    assert "+3" not in out  # 첫 파일 3번째 줄은 잘렸다
    assert "diff --git a/y.py" in out  # 두 번째 파일은 살아 있다


def test_truncate_per_commit_stops_early():
    out, cut = truncate_diff(TWO_FILES, max_per_file=100, max_per_commit=3)
    assert cut is True
    assert "커밋 diff 나머지 생략됨" in out


def test_truncate_within_limits_is_unchanged():
    out, cut = truncate_diff(TWO_FILES, max_per_file=100, max_per_commit=100)
    assert cut is False
    assert out == TWO_FILES


def test_truncate_empty():
    assert truncate_diff("", 10, 10) == ("", False)
    assert truncate_diff("   \n\n", 10, 10) == ("", False)


# --- 컬럼 순서 --------------------------------------------------------------


def test_columns_match_table_definition():
    """0001_init.sql 의 commits 컬럼과 순서까지 같아야 한다.

    어긋나면 타입이 맞는 한 에러 없이 잘못된 값이 들어간다 (repo_name 자리에 hash 등).
    """
    assert COLUMNS == [
        "commit_date",
        "repo_path",
        "repo_name",
        "commit_hash",
        "author_email",
        "message",
        "files",
        "diff",
        "diff_truncated",
        "authored_at",
    ]
