"""LLM 도구 (F2 확장) — 요약 중 LLM 이 스스로 호출하는 읽기 전용 조회 도구.

지금은 `get_full_diff` 하나다. diff 3단 상한(§8) 때문에 잘린 커밋의 전체 diff 를
LLM 이 필요할 때 직접 요청할 수 있게 한다. 쓰기 도구는 만들지 않는다 — 저장·렌더링은
코드가 담당해야 마스킹(D25)과 멱등성이 유지된다.

설계:
- **repo 경로는 LLM 에게 받지 않는다.** 그날 커밋의 `hash → repo_path` 맵을 코드가
  만들어 주입한다. LLM 이 경로를 넘기게 하면 없는 저장소를 추측해서 뒤지게 된다.
- **반환 직전 mask() 를 거친다.** 상한 처리 전 원문이 API 로 나가는 자리라
  `log_run` 과 같은 choke point 원리를 적용한다 (D25).
- **호출 횟수 상한.** LLM 이 조회를 반복하며 비용을 태우는 걸 코드가 막는다.
- **실패는 예외 대신 문자열로 반환한다.** tool_result 로 돌려줘야 LLM 이 읽고
  다른 방법을 찾는다. 예외를 던지면 그날 요약 전체가 failed 가 된다.
"""

from __future__ import annotations

import subprocess
import sys

from journal.collect_git import _git
from journal.mask import mask


def _log(msg: str) -> None:
    """도구 사용을 터미널에 보여준다. LLM 이 뭘 조회하는지 실행 중에 보이게.

    stderr 로 보낸다 — stdout 은 파이프라인 결과 출력용이라 섞이면 안 된다.
    flush 는 필수. LLM 호출 대기 중에 버퍼에 걸려 있으면 실시간으로 안 보인다.
    """
    print(f"    [도구] {msg}", file=sys.stderr, flush=True)

#: API 에 넘길 tool 정의. description 이 호출 조건을 정한다 —
#: "생략됨 표시가 있을 때만" 을 명시해야 멀쩡한 diff 까지 재조회하지 않는다.
TOOL_SCHEMA = {
    "name": "get_full_diff",
    "description": (
        "잘린 diff 의 전체 내용을 조회한다. 입력의 diff 에 '생략됨' 표시가 있고 "
        "그 내용이 요약 판단에 꼭 필요할 때만 호출한다. "
        "file_path 를 주면 그 파일의 diff 만 반환한다 (권장 — 전체 diff 는 클 수 있다)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "commit_hash": {
                "type": "string",
                "description": "입력에 `## 커밋 commit:{hash}` 로 표시된 커밋 해시",
            },
            "file_path": {
                "type": "string",
                "description": "저장소 루트 기준 상대 경로. 생략하면 커밋 전체 diff",
            },
        },
        "required": ["commit_hash"],
    },
}


class DiffTool:
    """하루치 요약 한 번에 하나씩 만들어 쓴다 — 호출 카운터가 그날 요약에만 걸리게.

    `repo_by_hash` 는 그날 커밋의 `commit_hash → repo_path` 맵. 호출부(summarize)가
    DB 에서 읽은 커밋 목록으로 만들어 넘긴다.
    """

    def __init__(self, repo_by_hash: dict[str, str], max_calls: int = 5, max_lines: int = 2000):
        self.repo_by_hash = repo_by_hash
        self.max_calls = max_calls
        self.max_lines = max_lines
        self.calls = 0

    def get_full_diff(self, commit_hash: str, file_path: str | None = None) -> str:
        """전체 diff 를 돌려준다. 실패·한도 초과도 전부 문자열로 — LLM 이 읽는 답이다."""
        target = f"{commit_hash} {file_path}" if file_path else f"{commit_hash} (전체)"
        _log(f"get_full_diff {target} — 호출 {self.calls + 1}/{self.max_calls}")

        if self.calls >= self.max_calls:
            _log("→ 한도 초과, 거부")
            return (
                f"오류: 조회 한도({self.max_calls}회)를 초과했다. "
                "지금까지 확보한 내용만으로 요약한다."
            )
        self.calls += 1

        repo = self.repo_by_hash.get(commit_hash)
        if repo is None:
            _log("→ 알 수 없는 커밋")
            return (
                f"오류: 알 수 없는 커밋 {commit_hash!r}. "
                "입력에 commit:{hash} 로 표시된 해시만 조회할 수 있다."
            )

        args = ["show", "--format=", "--patch", "--unified=3", commit_hash]
        if file_path:
            args += ["--", file_path]
        try:
            # *args 리스트의 안의 원소들을 낱개로 하는 리스트를 만들어줌.
            #subprocess 가 실행되어 diff을 patch에 넣어준다.
            patch = _git(repo, *args)
        except subprocess.CalledProcessError as e:
            _log("→ git 실패")
            # stderr 에 토큰 박힌 원격 URL 이 들어올 수 있다 — 마스킹 후 반환.
            return f"오류: git 조회 실패 — {mask(e.stderr) or '(상세 없음)'}"

        if not patch.strip():
            _log("→ 결과 없음")
            return "결과 없음 — 해당 커밋(또는 파일)에 diff 가 없다. file_path 오타일 수 있다."

        # 수집 상한과 별개의 도구 자체 상한. lockfile 처럼 거대한 diff 가
        # 통째로 컨텍스트에 들어가는 걸 막는다. 잘렸다는 사실은 텍스트에 남긴다 (§8).
        lines = patch.splitlines()
        if len(lines) > self.max_lines:
            _log(f"→ {len(lines)}줄 중 {self.max_lines}줄 반환 (상한 적용)")
            lines = lines[: self.max_lines] + [
                f"... (도구 상한 {self.max_lines}줄 초과, 나머지 생략됨)"
            ]
        else:
            _log(f"→ {len(lines)}줄 반환")

        return mask("\n".join(lines)) or ""
