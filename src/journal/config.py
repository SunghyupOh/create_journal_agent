"""설정 (스펙 §6).

`.env` 는 커밋하지 않는다 (NF-5). 여기에는 **비밀이 아닌 기본값만** 하드코딩하고,
비밀은 전부 환경변수에서 읽는다.

지금은 4단계(db.py)에 필요한 항목만 있다. 수집·요약 단계에서 항목이 추가된다.
"""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()  # 프로젝트 루트의 .env 를 환경변수로 올린다. 이미 있는 값은 덮어쓰지 않는다.

#: 모든 날짜 귀속의 기준. UTC 타임스탬프를 이 시간대로 바꿔 "며칠 일지"인지 정한다.
TIMEZONE = "Asia/Seoul"

#: 첫 실행 시 어느 날짜부터 훑을지. journals 에 행이 하나라도 있으면 무시된다 (dates.scan_from).
BOOTSTRAP_START = date(2026, 7, 19)


#: 일지 마크다운이 쌓이는 곳. 나중에(10+) 이 디렉터리를 private git 레포로 만든다.
JOURNAL_DIR = "~/dev-jounal/created_jounals"

#: 요약 LLM. 모델 ID는 입력 지문에 들어가므로 바꾸면 잠정 일지가 자동 재생성된다 (§4).
MODEL = "claude-sonnet-5"
LLM_MAX_TOKENS = 16000
LLM_EFFORT = "medium"  # 사실 추출·구조화 태스크라 깊은 추론 불필요. 품질 아쉬우면 high

#: `.git` 를 찾아 내려갈 루트들. **서로 중첩되면 안 된다** — 같은 저장소가 두 번 잡힌다.
#: (`dedupe_repo_roots` 가 심볼릭 링크는 잡아주지만 중첩은 못 잡는다.)
SCAN_ROOTS = [
    "~/projects",
    "~/ai_mentor",
    "~/dtartup-server",
    "~/dev-journal",
]

#: 커밋 필터는 **이름이 아니라 이메일**로. 한 사람이 여러 이름 표기를 쓴다
#: (`jooseong moon`/`JooseongMo0n`, `ohsunghyup`/`sunghyupoh`).
AUTHOR_EMAILS = [
    "tjdguq8042@gmail.com"
]

#: 저장소를 찾을 때 내려가지 않을 디렉터리. 숨김 디렉터리(`.` 시작)는 별도로 전부 제외한다
#: — `~/.nvm`, `~/.oh-my-zsh` 안에도 `.git` 이 있다.
EXCLUDE_DIRS = frozenset(
    {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target", "vendor"}
)

#: `.git` 을 찾아 내려갈 최대 깊이. 루트 자신이 0.
MAX_SCAN_DEPTH = 3

#: 클로드 코드 transcript 루트. 클로드 코드는 **Windows에서** 실행되므로 세션 파일은
#: Windows 홈에 있다 — WSL에서는 /mnt/c 로 접근한다. (WSL 쪽 ~/.claude 는 비어 있다.)
SESSIONS_DIR = "/mnt/c/Users/ohsunghyup/.claude/projects"

#: 세션 파일 mtime 컷 여유 (F1-2). 늘리면 느려질 뿐 틀리지 않고, 줄이면 조용히 빠진다.
SESSION_MTIME_MARGIN_DAYS = 2

# diff 3단 상한 (§8). 넘으면 잘라내고 `diff_truncated=True` + run_logs `warn`.
DIFF_MAX_LINES_PER_FILE = 200
DIFF_MAX_LINES_PER_COMMIT = 1000
DIFF_MAX_LINES_PER_DATE = 5000


def db_url() -> str:
    """Supabase 연결 문자열. 없으면 즉시 실패한다.

    상수가 아니라 함수인 이유: import 시점에 터지면 `.env` 없이도 되는 테스트까지
    같이 죽는다. 실제로 DB를 쓰는 순간에만 검사한다.
    """
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError(
            "SUPABASE_DB_URL 이 없다. .env.example 을 .env 로 복사해 채운다. "
            "(Supabase 대시보드 → Connection string → Session pooler)"
        )
    return url
