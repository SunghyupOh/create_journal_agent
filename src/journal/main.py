"""오케스트레이션 + CLI (§6). `journal run` 의 실체.

흐름 (§3):

    후보 날짜 계산 (dates) → 수집 (collect_git, collect_sessions)
    → 날짜별: 요약 (summarize) → 렌더링 (write)

여기는 **순서와 실패 처리만** 안다. 로직은 전부 각 모듈에 있다 — 이 파일 한 장이
전체 그림이다.

실패 격리: 한 날짜의 요약 실패가 다른 날짜를 막지 않는다. 실패한 날짜는 failed 로
남아 다음 실행이 재시도한다 (D16/D17).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from journal import collect_git, collect_sessions, config, dates, db, progress, summarize, write


def run(max_days: int | None = None) -> int:
    """파이프라인 1회 실행. 실패한 날짜 수를 반환한다 (종료 코드용).

    `today` 는 시스템 시간이 아니라 **Asia/Seoul 기준** — 서버가 UTC 로 돌아도
    자정 직후에 "어제"를 오늘로 처리하는 어긋남이 없다.
    """
    # dhsmf rPtks.
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()

    with db.connect() as conn:
        # (날짜, 상태) 값을 db에서 가져온다.
        rows = db.fetch_journal_status(conn)
        # 저널 테이블에서 가져온 행에서 일지 생성 후부 날짜 달력을 가져옴
        candidates = dates.candidate_dates(rows, today, config.BOOTSTRAP_START)
        # 일지 작성으로 정한 날짜 , 범위에서 아직 to do로 취하지 않은 날 개수 반환 => take()
        todo, remaining = dates.take(candidates, max_days)

        if not todo:
            print("처리할 날짜가 없다.")
            return 0

        print(f"대상: {todo[0]} ~ {todo[-1]} ({len(todo)}일" + (f", {remaining}일 남음)" if remaining else ")"))

        # 그 day : 개수 를 원소로 갖는 리스트 counts
        with progress.spinner("git 커밋 수집 중"):
            git_counts = collect_git.collect(conn, todo)
        print(f"수집: git 커밋 {sum(git_counts.values())}건")

        with progress.spinner("클로드 세션 수집 중"):
            session_counts = collect_sessions.collect(conn, todo)
        print(f"수집: 세션 {sum(session_counts.values())}건")

        failed = 0
        for day in todo:
            try:
                #summarize_day -> LLM 호출해서 요약본 만들고 db에 저장 + status(failed, skipped 등) 반환
                with progress.spinner(f"{day} 요약 중 (LLM 응답 대기)"):
                    status = summarize.summarize_day(conn, day, today)
            except Exception as e:
                # summarize_day 가 이미 failed 마킹 + 로그를 남겼다. 다음 날짜로.
                print(f"  {day}: 실패 — {e}", file=sys.stderr)
                failed += 1
                continue
            # skipped 이 아닌 일지 마크 다운 파일로 저장.
            # DB에 저널 테이블 summary_json에 day 값으로 가져옴 => write_day()
            path = write.write_day(conn, day) if status != "skipped" else None
            print(
                f"  {day}: {status}"
                f" (커밋 {git_counts.get(day, 0)}, 세션 {session_counts.get(day, 0)})"
                + (f" → {path}" if path else "")
            )

        if remaining:
            print(f"{remaining}일 남음 — 다시 실행하면 이어서 처리된다.")
        return failed

# 터미널에서 사용가능한 명령어를 등록한다. juornal이라는 실행 파일이 어딘가에 있고,
# 셸이 그걸 찾아 실행하고 이후에 오는 run, 2 등 문자열을 그 파일에 넘겨주는 것임.
def cli() -> None:
    parser = argparse.ArgumentParser(prog="journal", description="개발 일지 파이프라인")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="수집 → 요약 → 렌더링")
    p_run.add_argument(
        "--max-days", type=int, default=None,
        help="이번 실행에서 처리할 최대 날짜 수 (LLM 비용 제한). 나머지는 다음 실행에서",
    )

    p_web = sub.add_parser("web", help="로컬 웹 UI (실행 버튼 + step 표시 + 일지 뷰어)")
    p_web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.command == "run":
        sys.exit(1 if run(max_days=args.max_days) else 0)
    elif args.command == "web":
        import uvicorn  # web 안 쓰는 실행까지 느려지지 않게 여기서 import

        print(f"http://127.0.0.1:{args.port} — Ctrl+C 로 종료")
        uvicorn.run("journal_web.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    cli()
