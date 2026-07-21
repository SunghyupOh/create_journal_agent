"""요약 (F2) — 파이프라인에서 유일하게 LLM 을 부르는 자리.

하루치 commits + session_extracts 를 DB에서 읽어 → 프로젝트별로 그룹핑(D13 ⓐ) →
LLM 한 번 호출 → 결과를 journals 에 저장한다.

이 단계(7단계)는 정상 경로만 구현한다 — sources 검증, 입력 지문, map-reduce 는
10+ 단계에서 붙인다. 가드를 먼저 넣으면 "요약이 이상한 건지 가드가 이상한 건지"를
동시에 디버깅하게 된다 (§7-C).

두 소스의 연결(D13):
    ⓐ 프로젝트 그룹핑 — 코드가 결정적으로. 세션 cwd 가 어느 저장소 루트 아래인지.
    ⓑ 파일 겹침·브랜치 — 입력 구조에 신호만 담고 LLM 이 의미로 연결.
코드로 결정적 매칭기를 만들지 않는다 — 같은 파일을 건드린 무관한 작업을 오결합할 위험.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import anthropic
import psycopg

from journal import config, db, prompts, tools
from journal.paths import find_repo_root
from journal.schema import JournalSummary


@dataclass
class DayInput:
    """하루치 수집 데이터 — DB 행을 그대로 들지 않고 필요한 필드만."""

    commits: list[tuple]  # (repo_path, repo_name, commit_hash, message, files, diff, diff_truncated)
    extracts: list[tuple]  # (session_id, project_dir, git_branch, text, edited_files)

#DB에서 그날 커밋, 세션 내용 가져옴
def load_day(conn: psycopg.Connection, day: date) -> DayInput:
    with conn.cursor() as cur:
        cur.execute(
            """select repo_path, repo_name, commit_hash, message, files, diff, diff_truncated
               from commits where commit_date = %s order by authored_at""",
            (day,),
        )
        commits = cur.fetchall()
        cur.execute(
            """select session_id, project_dir, git_branch, text, edited_files
               from session_extracts where extract_date = %s""",
            (day,),
        )
        extracts = cur.fetchall()
    return DayInput(commits=commits, extracts=extracts)


# --- 그룹핑 + 입력 조립 (순수 함수) ------------------------------------------


#이 모듈의 핵심 그 날 한 세션, 깃 커밋 내용 그룹화한 텍스트를 반환
def build_input(data: DayInput) -> str:
    """프로젝트별로 커밋·세션을 묶어 LLM 입력 텍스트를 만든다.

    그룹 키는 저장소 루트 경로. 세션은 `project_dir`(정규화된 cwd)가 어느 저장소
    아래인지로 배정한다 — `paths.find_repo_root` 재사용. 어디에도 안 속하면 "기타".
    """
    #c[0] = repo_path, 원소가 문자열, git의 저장소 주소인 set 사용.
    repo_roots = sorted({c[0] for c in data.commits})

    # {루트 or None: {"commits": [...], "extracts": [...]}}
    groups: dict[str | None, dict[str, list]] = {}

    def bucket(key: str | None) -> dict[str, list]:
        return groups.setdefault(key, {"commits": [], "extracts": []})

    #주소 값을 키값으로 해당하는 커밋, 세션 내용을 넣는다.
    # 그 날 커밋한 저장소 주소를 그룹의 기준으로 삼고, 그거에 맞춰서 세션 내용을 분류하는 메커니즘
    for c in data.commits:
        bucket(c[0])["commits"].append(c)
    for e in data.extracts:
        # 저장소 roots 들 중 어디 아래 있는 건지 찾아서 버킷 키로 사용해 해당하는 세션 내용넣음
        # 결과적으로 같은 프로젝트 단위로 같은 그룹으로 묶임.
        root = find_repo_root(e[1], repo_roots) if e[1] else None
        bucket(root)["extracts"].append(e)

    parts: list[str] = []
    for root in sorted(groups, key=lambda r: (r is None, r or "")):
        g = groups[root]
        #name = repo_name
        name = g["commits"][0][1] if g["commits"] else (root or "기타")
        parts.append(f"# 프로젝트: {name}")

        for repo_path, _, commit_hash, message, files, diff, truncated in g["commits"]:
            parts.append(f"## 커밋 commit:{commit_hash}")
            parts.append(message)
            if files:
                parts.append(f"[변경 파일]\n{files}")
            if diff:
                note = " (일부 생략됨)" if truncated else ""
                parts.append(f"[diff{note}]\n{diff}")

        for session_id, _, branch, text, edited_files in g["extracts"]:
            parts.append(f"## 세션 session:{session_id}")
            if branch:
                parts.append(f"[브랜치] {branch}")
            if edited_files:
                parts.append(f"[수정 파일]\n{edited_files}")
            if text:
                parts.append(text)

    return "\n\n".join(parts)


# --- LLM 호출 ----------------------------------------------------------------


#: tool 루프 상한. DiffTool 자체 한도(5회)보다 넉넉하게 — 한 응답에 tool 호출이
#: 여러 개 올 수 있어서 턴 수 ≠ 호출 수다. 여기 걸리면 뭔가 잘못된 거라 실패 처리.
_MAX_TURNS = 8


def call_llm(day: date, grouped_input: str, diff_tool: tools.DiffTool) -> JournalSummary:
    """비스트리밍 호출 + tool 루프 (§4). 스키마 준수는 API 가 보장한다 (D23).

    LLM 이 잘린 diff 를 더 봐야겠다고 판단하면 `stop_reason == "tool_use"` 로 멈춘다.
    그러면 도구를 실행하고 결과를 대화에 붙여 재호출한다 — tool 을 안 쓰는 날은
    기존과 똑같이 1회 호출로 끝난다.

    `stop_reason` 이 refusal/max_tokens 면 스키마 보장이 깨지므로 실패로 처리 —
    예외를 던지면 호출부가 journals 를 failed 로 마킹하고 다음 실행이 재시도한다 (D17).
    """
    client = anthropic.Anthropic(max_retries=3)
    # diff_tool 은 호출부(summarize_day)가 만들어 넘긴다 — 루프가 끝난 뒤에도
    # 호출부가 diff_tool.calls 를 읽어 run_logs 에 남길 수 있게 하기 위해서다.
    # 루프를 돌며 assistant 응답과 tool 결과가 뒤에 계속 붙는다 — API 는 무상태라
    # 매 호출마다 대화 전체를 다시 보낸다.
    #
    # cache_control: "여기까지(tools + system + 이 메시지)를 캐시해라" 표시.
    # tool 루프 2턴째부터 이 구간(하루치 입력 전체)이 ~10% 가격으로 처리된다.
    # tool 을 안 쓰는 날은 1회 호출로 끝나 캐시 읽기가 없다 — 쓰기 1.25배만 내지만
    # 하루 1회 파이프라인이라 프리픽스 재사용처가 루프뿐이니 감수한다.
    messages: list = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompts.user_input(day.isoformat(), grouped_input),
                    "cache_control": {"type": "ephemeral"},
                    #cache_control 이 api 서버에서 체크 포인트로 작용해 이전 텍스트를 캐싱한다.
                }
            ],
        }
    ]

    for _ in range(_MAX_TURNS):
        response = client.messages.parse(
            model=config.MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": config.LLM_EFFORT},
            system=prompts.SYSTEM,
            messages=messages,
            tools=[tools.TOOL_SCHEMA],
            output_format=JournalSummary,
        ) # 이 인자 값들로 api서버는 컨텍스트를 조립하고 모델 응답을 반환해줌. 

        if response.stop_reason == "end_turn":
            return response.parsed_output
        if response.stop_reason != "tool_use":
            raise RuntimeError(f"LLM 비정상 종료: stop_reason={response.stop_reason!r}")

        # assistant 턴(tool_use 블록 포함)을 그대로 붙여야 다음 호출이 이어진다.
        messages.append({"role": "assistant", "content": response.content})

        # 한 응답에 tool_use 가 여러 개일 수 있다 — 전부 실행해서
        # tool_result 를 **한 user 메시지에** 모아 보낸다 (나눠 보내면 안 됨).
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            # block.input 은 LLM 이 만든 값이라 **kwargs 로 풀지 않는다 —
            # 예상 밖 키가 오면 TypeError 로 그날 요약이 통째로 죽는다.
            out = diff_tool.get_full_diff(
                block.input.get("commit_hash", ""), block.input.get("file_path")
            )
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"tool 루프가 {_MAX_TURNS}턴 안에 끝나지 않음")


# --- 저장 --------------------------------------------------------------------


def save(conn: psycopg.Connection, day: date, summary: JournalSummary | None, status: str) -> None:
    """journals upsert. summary 가 None 이면 skipped(활동 없음) 또는 failed.

    날짜당 한 행이라 REPLACE 대신 `on conflict` upsert.
    """
    summary_json = summary.model_dump_json() if summary is not None else None
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into journals (journal_date, summary_json, prompt_version, model, status)
                values (%s, %s, %s, %s, %s)
                on conflict (journal_date)
                do update set summary_json = excluded.summary_json,
                              prompt_version = excluded.prompt_version,
                              model = excluded.model,
                              status = excluded.status
                """,
                (day, summary_json, prompts.PROMPT_VERSION, config.MODEL, status),
            )


def summarize_day(conn: psycopg.Connection, day: date, today: date) -> str:
    """하루치 요약 전체 흐름. 최종 status 를 반환한다.

    - 수집 데이터가 아예 없으면 → skipped (LLM 호출 없음)
    - 과거 날짜 → done, 오늘 → provisional (아직 하루가 안 끝남, 내일 재요약된다)
    - 실패 → failed 로 마킹 후 예외 재전파. 다음 실행이 재시도한다 (D16/D17)
    """
    data = load_day(conn, day)
    if not data.commits and not data.extracts:
        save(conn, day, None, "skipped")
        db.log_run(conn, "summarize", "ok", journal_date=day, detail="활동 없음 → skipped")
        return "skipped"

    # DiffTool 용 hash → repo 경로 맵. LLM 에게 경로를 맡기지 않고 코드가 역참조한다.
    repo_by_hash = {c[2]: c[0] for c in data.commits}
    # 여기서 만들어 넘기는 이유: 요약이 끝난 뒤 diff_tool.calls 로
    # "도구를 몇 번 썼는지"를 run_logs 에 남기기 위해 (관측성).
    diff_tool = tools.DiffTool(repo_by_hash)

    try:
        summary = call_llm(day, build_input(data), diff_tool)
    except Exception as e:
        save(conn, day, None, "failed")
        db.log_run(conn, "summarize", "failed", journal_date=day, detail=str(e))
        raise

    status = "provisional" if day == today else "done"
    save(conn, day, summary, status)
    db.log_run(
        conn, "summarize", "ok", journal_date=day,
        detail=f"work {len(summary.work)}, trouble {len(summary.troubleshooting)},"
               f" pending {len(summary.pending)}, tool {diff_tool.calls}회",
    )
    return status
