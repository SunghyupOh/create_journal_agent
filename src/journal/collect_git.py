"""로컬 git 커밋 수집 (F1-1).

`SCAN_ROOTS` 아래 `.git` 을 찾아 → 날짜별 `git log` → 마스킹 → `commits` 테이블 날짜별 REPLACE.

날짜 귀속은 **author date 기준**이다. committer date는 rebase 때 갱신돼서 과거 일지가 흔들린다.
git의 `--since/--until` 타임존 해석에 기대지 않고 **±1일 넉넉히 뽑은 뒤 Python에서
Asia/Seoul로 변환해 거른다** (§8).

단일 환경 전제 (D12) — 다른 기기 커밋은 수집되지 않는다. 여러 기기에서 돌리면 날짜별
REPLACE가 서로의 커밋을 지운다.
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from journal import config, db
from journal.mask import mask
from journal.paths import dedupe_repo_roots, to_posix

#: `commits` 테이블 컬럼 순서. `replace_for_date` 에 넘기는 튜플과 **반드시** 같은 순서여야 한다.
#: 어긋나도 타입이 맞으면 에러 없이 잘못 들어간다.
COLUMNS = [
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

# git log 출력 구분자. 커밋 메시지에 들어갈 수 없는 제어문자를 쓴다 —
# 개행이나 `|` 로 나누면 메시지 본문이 여러 줄일 때 파싱이 깨진다.
_REC = "\x1e"  # 커밋 사이
_SEP = "\x1f"  # 필드 사이


# 필드값만 나열하면 생성자,__eq__,repr 을 자동 생성해주는 데코레이터
@dataclass(frozen=True)
class Commit:
    commit_date: date
    repo_path: str
    repo_name: str
    commit_hash: str
    author_email: str
    message: str
    files: str
    diff: str
    diff_truncated: bool
    authored_at: datetime


# --- 저장소 발견 ------------------------------------------------------------


def discover_repos(scan_roots: list[str] | None = None) -> list[str]:
    """스캔 루트 아래 `.git` 을 가진 디렉터리를 찾아 realpath 정규화 + 중복 제거.

    숨김 디렉터리는 내려가지 않는다 — `~/.nvm`, `~/.oh-my-zsh` 안에도 `.git` 이 있어서
    그대로 두면 남의 저장소 커밋이 일지에 섞인다.
    """
    roots = scan_roots if scan_roots is not None else config.SCAN_ROOTS
    found: list[str] = []

    for root in roots:
        base = os.path.realpath(os.path.expanduser(root))
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip("/").count("/")

        for dirpath, dirnames, _ in os.walk(base):
            if os.path.exists(os.path.join(dirpath, ".git")):
                found.append(dirpath)

            if dirpath.rstrip("/").count("/") - base_depth >= config.MAX_SCAN_DEPTH:
                dirnames.clear()  # 더 내려가지 않는다
                continue
            # dirnames 를 **제자리에서** 고쳐야 os.walk 가 그 디렉터리를 건너뛴다.
            # 새 리스트를 대입하면(`dirnames = [...]`) 아무 효과가 없다.
            dirnames[:] = [
                d for d in dirnames if not d.startswith(".") and d not in config.EXCLUDE_DIRS
            ]

    return dedupe_repo_roots(found)


# --- git 실행 ---------------------------------------------------------------


def _git(repo: str, *args: str) -> str:
    """저장소에서 git 명령 실행. 실패하면 `CalledProcessError`.

    `cwd=repo` 로 실행한다 — `-C` 옵션과 같지만 경로가 인자에 섞이지 않아 읽기 쉽다.
    `errors="replace"` 는 커밋 메시지에 깨진 인코딩이 있어도 죽지 않게 한다.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


# --- diff 상한 --------------------------------------------------------------


def _split_by_file(patch: str) -> list[str]:
    """`diff --git` 헤더 기준으로 파일별 블록 분리. blocks에 담겨서 반환됨. current는
    블럭을 잠시 저장하는 중간 저장소."""
    blocks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git ") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def truncate_diff(patch: str, max_per_file: int, max_per_commit: int) -> tuple[str, bool]:
    """파일당·커밋당 상한을 적용한다. `(잘린 diff, 잘렸는지)`.
    한 커밋에 여러 파일이 포함 될 수 있으니 각각에 상한을 검.
    out = 잘린 diff 문자열이 저장됨. trucated = diff이 상한을 넘겨서 잘렸는지 여부(boolean)

    잘렸다는 사실을 **텍스트 안에 남긴다** — LLM이 "이게 전부"라고 믿고 없는 내용을
    추측하지 않게 하려는 것이다 (§8).

    >>> out, cut = truncate_diff("a\\nb\\nc\\nd", max_per_file=2, max_per_commit=100)
    >>> cut
    True
    """
    if not patch.strip():
        return "", False

    out: list[str] = []
    total = 0
    truncated = False

    for block in _split_by_file(patch):
        lines = block.splitlines()
        if len(lines) > max_per_file:
            dropped = len(lines) - max_per_file
            lines = lines[:max_per_file] + [f"... (이 파일 diff {dropped}줄 생략됨)"]
            truncated = True

        if total + len(lines) > max_per_commit:
            room = max_per_commit - total
            if room > 0:
                out.extend(lines[:room])
                total += room
            out.append("... (커밋 diff 나머지 생략됨)")
            truncated = True
            break

        out.extend(lines)
        total += len(lines)

    return "\n".join(out), truncated


# --- 커밋 조회 --------------------------------------------------------------


def _to_local_date(iso: str, tz: ZoneInfo) -> tuple[datetime, date]:
    """git의 `%aI`(오프셋 포함 ISO8601) → (원본 datetime, 로컬 날짜)."""
    dt = datetime.fromisoformat(iso)
    return dt, dt.astimezone(tz).date()


def fetch_commits(repo: str, days: list[date], emails: list[str] | None = None) -> list[Commit]:
    """`repo` 에서 `days` 에 해당하는 커밋들을 뽑는다.

    `--all` 은 필수다 — 기본 `git log` 는 HEAD 기준이라 브랜치를 옮겨두면 누락된다.
    `--no-merges` 는 일지 가치가 없는 머지 커밋을 뺀다.
    repo : 저장소, days: 이전 dates.py에서 만들어지는 일지 작성 후보 날짜.
    commit 객체를 가지는 commits리스트를 반환 해줌.
    """
    if not days:
        return []

    authors = emails if emails is not None else config.AUTHOR_EMAILS
    tz = ZoneInfo(config.TIMEZONE)
    repo_path = to_posix(repo)
    repo_name = os.path.basename(repo_path)
    wanted = set(days)

    # ±1일 여유. git의 타임존 해석에 기대지 않고 Python에서 정확히 거른다.
    since = (min(days) - timedelta(days=1)).isoformat()
    until = (max(days) + timedelta(days=2)).isoformat()

    raw = _git(
        repo,
        "log",
        "--all",
        "--no-merges",
        f"--since={since}",
        f"--until={until}",
        *[f"--author={e}" for e in authors],
        f"--pretty=format:{_REC}%h{_SEP}%ae{_SEP}%aI{_SEP}%B",
    )

    commits: list[Commit] = []
    for record in raw.split(_REC):
        if not record.strip():
            continue
        short_hash, email, iso, message = record.split(_SEP, 3)
        authored_at, local_date = _to_local_date(iso, tz)
        if local_date not in wanted:
            continue

        files = _git(repo, "show", "--format=", "--name-only", short_hash).strip()
        patch = _git(repo, "show", "--format=", "--patch", "--unified=3", short_hash)
        diff, truncated = truncate_diff(
            patch, config.DIFF_MAX_LINES_PER_FILE, config.DIFF_MAX_LINES_PER_COMMIT
        )

        commits.append(
            Commit(
                commit_date=local_date,
                repo_path=repo_path,
                repo_name=repo_name,
                commit_hash=short_hash,
                author_email=email,
                message=mask(message.strip()) or "",
                files=files,
                diff=mask(diff) or "",
                diff_truncated=truncated,
                authored_at=authored_at,
            )
        )

    return commits


# --- 저장 -------------------------------------------------------------------


def _rows_for_date(commits: list[Commit]) -> tuple[list[tuple], bool]:
    """하루치 커밋을 삽입용 튜플로. 날짜 총량 상한을 여기서 적용한다.

    오래된 커밋부터 채우고 넘치면 이후 커밋의 diff를 버린다 — 커밋 자체는 남긴다.
    메시지와 파일 목록만으로도 "무엇을 했는지"는 요약된다.
    """
    rows: list[tuple] = []
    total = 0
    date_truncated = False

    for c in sorted(commits, key=lambda x: x.authored_at):
        lines = c.diff.count("\n") + 1 if c.diff else 0
        if total + lines > config.DIFF_MAX_LINES_PER_DATE:
            diff, truncated = "... (하루 diff 총량 상한 초과로 생략됨)", True
            date_truncated = True
        else:
            diff, truncated = c.diff, c.diff_truncated
            total += lines

        #rows = 컬럼 10개짜리 튜플(DB의 한 행)을 원소로 하는 list.
        rows.append(
            (
                c.commit_date,
                c.repo_path,
                c.repo_name,
                c.commit_hash,
                c.author_email,
                c.message,
                c.files,
                diff, # diff 만 상한을 적용함.
                truncated,
                c.authored_at,
            )
        )

    return rows, date_truncated


def collect(conn: psycopg.Connection, days: list[date]) -> dict[date, int]:
    """`days` 의 커밋을 수집해 날짜별로 REPLACE 한다. `{날짜: 커밋 수}` 반환.

    저장소 하나가 깨져도 나머지는 계속 수집한다 — 한 프로젝트의 git 문제로 그날 일지
    전체를 잃을 이유가 없다. 실패는 `run_logs` 에 `warn` 으로 남는다.
    """
    by_date: dict[date, list[Commit]] = defaultdict(list)

    for repo in discover_repos():
        try:
            for c in fetch_commits(repo, days):
                by_date[c.commit_date].append(c)
        except subprocess.CalledProcessError as e:
            # stderr 에 토큰이 박힌 원격 URL이 들어올 수 있다. log_run 이 마스킹한다.
            db.log_run(conn, "collect_git", "warn", detail=f"{repo}: {e.stderr}")

    counts: dict[date, int] = {}
    for day in days:
        #하루 단위로 row 수집 -> 쿼리 실행.
        rows, date_truncated = _rows_for_date(by_date.get(day, []))
        # 커밋이 0건이어도 REPLACE 한다 — 지난 실행이 남긴 행을 지워야 멱등이다.
        counts[day] = db.replace_for_date(conn, "commits", "commit_date", day, COLUMNS, rows)
        # replace_for_date 함수로 DB 쿼리 날림.
        if date_truncated:
            db.log_run(
                conn, "collect_git", "warn", journal_date=day, detail="하루 diff 총량 상한 초과"
            )

    return counts
