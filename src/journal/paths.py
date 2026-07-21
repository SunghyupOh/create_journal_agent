"""경로 정규화 (D22).

transcript(클로드 코드)와 git이 서로 다른 표기를 쓴다:

    transcript.cwd        \\\\wsl.localhost\\Ubuntu\\home\\oh\\ai_mentor\\backend
    transcript.file_path  \\\\wsl.localhost\\Ubuntu\\home\\oh\\ai_mentor\\backend\\llm\\client.py
    git repo_path         /home/oh/ai_mentor
    git files             backend/llm/client.py

정규화하지 않으면 F2-2 프로젝트 그룹핑이 전부 "기타"로 떨어지고 커밋↔세션 파일 겹침
신호(D13)가 한 건도 맞지 않는다. 에러가 아니라 품질 저하로 나타나므로 조용히 깨진다.

realpath()·dedupe_repo_roots()만 파일 시스템을 만진다. 나머지는 순수 함수다.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

# \\wsl.localhost\<distro>\  와 구형 \\wsl$\<distro>\  둘 다 처리한다.
# config에 접두사를 고정하지 않는 이유: 배포판 이름이 바뀌거나 두 표기가 섞여도 그대로 동작한다.
_WSL_UNC = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\", re.IGNORECASE)


def to_posix(path: str | None) -> str | None:
    r"""transcript의 UNC/윈도우 경로를 POSIX 표기로 바꾼다.

    >>> to_posix(r"\\wsl.localhost\Ubuntu\home\oh\ai_mentor")
    '/home/oh/ai_mentor'
    >>> to_posix("/home/oh/ai_mentor")
    '/home/oh/ai_mentor'
    >>> to_posix(r"C:\Users\oh\proj")
    'C:/Users/oh/proj'

    WSL 밖 경로(네이티브 윈도우)를 억지로 POSIX처럼 만들지 않는다. 어차피 어느 저장소에도
    속하지 않아 "기타" 버킷으로 가야 하는데, `/c/Users/...` 로 바꾸면 저장소 루트와
    우연히 겹칠 여지만 생긴다. `~/.claude/projects/` 에 `C--Users-...` 세션이 실재한다.
    """
    if not path:
        return path

    if _WSL_UNC.match(path):
        path = "/" + _WSL_UNC.sub("", path)

    path = path.replace("\\", "/")

    # 중복 슬래시 정리. 선행 "//" 는 남은 UNC 호스트일 수 있어 건드리지 않는다.
    path = re.sub(r"(?<!^)/{2,}", "/", path)

    # 루트가 아닌 경로의 후행 슬래시 제거 — 비교 시 어긋나는 원인.
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    return path


def is_within(path: str, root: str) -> bool:
    """`path` 가 `root` 아래(자기 자신 포함)인가. 경로 **경계** 기준으로 판정한다.

    문자열 접두사로 비교하면 `/home/oh/ai_mentor_backup` 이 `/home/oh/ai_mentor` 에
    속한다고 잘못 판정된다.

    >>> is_within("/home/oh/ai_mentor/backend", "/home/oh/ai_mentor")
    True
    >>> is_within("/home/oh/ai_mentor_backup", "/home/oh/ai_mentor")
    False
    >>> is_within("/home/oh/ai_mentor", "/home/oh/ai_mentor")
    True
    """
    p = PurePosixPath(path).parts
    r = PurePosixPath(root).parts
    return len(p) >= len(r) and p[: len(r)] == r


def to_repo_relative(path: str, repo_root: str) -> str:
    """저장소 루트 기준 상대경로로. 저장소 밖이면 **원본을 그대로** 돌려준다.

    git의 `files` 가 저장소 상대경로이므로 세션 쪽 절대 경로도 여기까지 맞춰야
    겹침 신호가 성립한다.

    >>> to_repo_relative("/home/oh/ai_mentor/backend/llm/client.py", "/home/oh/ai_mentor")
    'backend/llm/client.py'
    >>> to_repo_relative("/home/oh/other/x.py", "/home/oh/ai_mentor")
    '/home/oh/other/x.py'
    """
    if not is_within(path, repo_root):
        return path
    rel = str(PurePosixPath(path).relative_to(PurePosixPath(repo_root)))
    return "" if rel == "." else rel


def find_repo_root(path: str, repo_roots: list[str]) -> str | None:
    """`path` 를 담는 **최근접 상위** 저장소 루트 주소를 반환. 없으면 None ("기타" 버킷).

    중첩 저장소가 있으면 더 깊은 쪽이 이긴다.

    >>> roots = ["/home/oh/ai_mentor", "/home/oh/ai_mentor/vendor/lib"]
    >>> find_repo_root("/home/oh/ai_mentor/vendor/lib/a.py", roots)
    '/home/oh/ai_mentor/vendor/lib'
    >>> find_repo_root("/home/oh/scratch", roots) is None
    True
    """
    matches = [r for r in repo_roots if is_within(path, r)]
    if not matches:
        return None
    return max(matches, key=lambda r: len(PurePosixPath(r).parts))


def realpath(path: str) -> str:
    """`~` 확장 + 심볼릭 링크 해제 + POSIX 정규화 (파일 시스템 접근).

    저장소 발견 단계에서만 쓴다. 같은 저장소가 두 경로로 잡히면 커밋이 중복 수집되는데,
    `unique (repo_path, commit_hash)` 는 repo_path 가 달라 이를 막지 못한다.
    """
    return to_posix(os.path.realpath(os.path.expanduser(path)))


def dedupe_repo_roots(paths: list[str]) -> list[str]:
    """realpath 정규화 후 중복 제거. 입력 순서는 유지한다.

    SCAN_ROOTS 가 중첩되거나(`~` 와 `~/projects`) 심볼릭 링크가 걸려 있으면 같은
    저장소가 여러 번 발견된다.
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        rp = realpath(p)
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out
