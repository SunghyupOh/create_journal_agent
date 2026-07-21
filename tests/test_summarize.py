"""summarize.py 중 순수 함수 부분 — 그룹핑과 입력 조립. LLM·DB 없이 검증."""

from journal.summarize import DayInput, build_input

COMMIT = (
    "/home/oh/ai_mentor",  # repo_path
    "ai_mentor",  # repo_name
    "a1b2c3",  # commit_hash
    "fix: 토큰 만료 검사",  # message
    "backend/auth.py",  # files
    "diff --git ...",  # diff
    False,  # diff_truncated
)
EXTRACT_SAME_REPO = (
    "sess-1",  # session_id
    "/home/oh/ai_mentor/backend",  # project_dir — 저장소 하위 디렉터리
    "main",  # git_branch
    "[사용자] 만료 검사 왜 안 돼?",  # text
    "backend/auth.py",  # edited_files
)
EXTRACT_OTHER = ("sess-2", "C:/study/jpa", None, "[사용자] JPA 질문", "")


def test_session_grouped_into_repo_by_cwd():
    """cwd 가 저장소 하위면 그 저장소 그룹에 들어간다 (D13 ⓐ)."""
    out = build_input(DayInput(commits=[COMMIT], extracts=[EXTRACT_SAME_REPO]))
    project_block = out.split("# 프로젝트: ")[1]
    assert "commit:a1b2c3" in project_block
    assert "session:sess-1" in project_block  # 같은 블록 안


def test_unmatched_session_goes_to_etc():
    out = build_input(DayInput(commits=[COMMIT], extracts=[EXTRACT_OTHER]))
    assert "# 프로젝트: 기타" in out
    etc_block = out.split("# 프로젝트: 기타")[1]
    assert "session:sess-2" in etc_block
    assert "commit:a1b2c3" not in etc_block


def test_sources_tokens_present():
    """LLM 이 sources 에 쓸 토큰이 입력에 그대로 있어야 한다."""
    out = build_input(DayInput(commits=[COMMIT], extracts=[EXTRACT_SAME_REPO]))
    assert "commit:a1b2c3" in out
    assert "session:sess-1" in out


def test_truncated_diff_is_labeled():
    """잘린 diff 는 입력에 표시된다 — LLM 이 없는 내용을 추측하지 않게 (§8)."""
    cut = COMMIT[:6] + (True,)
    out = build_input(DayInput(commits=[cut], extracts=[]))
    assert "[diff (일부 생략됨)]" in out


def test_empty_day():
    assert build_input(DayInput(commits=[], extracts=[])) == ""


def test_commits_only_no_sessions():
    out = build_input(DayInput(commits=[COMMIT], extracts=[]))
    assert "# 프로젝트: ai_mentor" in out


def test_branch_and_files_signals_included():
    """연결 신호 ⓑ — 브랜치·수정 파일이 입력 구조에 담긴다 (D13)."""
    out = build_input(DayInput(commits=[COMMIT], extracts=[EXTRACT_SAME_REPO]))
    assert "[브랜치] main" in out
    assert "[수정 파일]\nbackend/auth.py" in out
