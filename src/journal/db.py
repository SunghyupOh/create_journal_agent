"""Postgres 연결 + 날짜별 REPLACE 헬퍼 (§5, D24).

PostgREST(`supabase-py`)가 아니라 `psycopg` 직접 연결을 쓴다. 날짜 단위 통째 교체는
**삭제 + 삽입**인데, PostgREST는 둘이 별개의 HTTP 요청이라 트랜잭션 경계를 잡을 수 없다.
그 사이에서 죽으면 그 날짜 데이터가 사라진 상태로 남는다.

수집 단계가 이 모듈에서 쓰는 건 사실상 `replace_for_date()` 하나다. 이 함수가
멱등성(D5)을 구현한 자리다 — 몇 번을 다시 돌려도 결과가 같다.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from typing import Any

#TCP 연결 인증, postgres 전용 프로토콜 사용.
import psycopg
from psycopg import sql

from journal import config
from journal.mask import mask

#: run_logs.step / status 허용값. DB의 check 제약과 **반드시** 일치해야 한다.
#: 어긋나면 삽입이 런타임에 실패한다 — 여기서 먼저 걸러 어느 쪽이 틀렸는지 바로 보이게 한다.
STEPS = frozenset({"collect_git", "collect_sessions", "summarize", "write"})
LOG_STATUSES = frozenset({"ok", "retry", "failed", "warn"})


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """연결 하나를 열고 끝나면 닫는다.

    연결 풀을 쓰지 않는다 — 이 프로그램은 하루 몇 번 돌고 끝나는 CLI라 동시 연결이 없다.
    풀은 관리 대상만 늘린다. 나중에 웹 UI를 붙이면 그때 `psycopg_pool` 로 바꾼다.
    """
    conn = psycopg.connect(config.db_url())
    # autocommit 필수. psycopg 는 기본이 비-autocommit이라 SELECT 하나만 실행돼도
    # 암묵적 트랜잭션이 열리고, 그 뒤의 `with conn.transaction():` 은 진짜 COMMIT 이
    # 아니라 SAVEPOINT 가 된다 — commit 없이 close 하면 **전부 롤백**된다.
    # 실제로 journal run 전체 결과가 조용히 사라졌던 버그. autocommit 이면 단문은
    # 즉시 확정되고, transaction() 블록은 명시적 BEGIN/COMMIT 으로 원자성을 유지한다.
    conn.autocommit = True
    # Transaction pooler(6543 포트)는 prepared statement를 지원하지 않는다.
    # psycopg는 같은 쿼리를 5번 실행하면 자동으로 prepare하는데, 그 순간부터
    # "prepared statement does not exist" 로 터진다. 꺼둔다.
    # (Session pooler(5432)로 붙을 땐 없어도 되지만 있어도 무해하다.)
    conn.prepare_threshold = None
    try:
        yield conn
    finally:
        conn.close()


def replace_sql(table: str, date_column: str, columns: Sequence[str]) -> tuple[sql.Composed, sql.Composed]:
    """`(delete문, insert문)` 을 만든다. 식별자는 `sql.Identifier` 로 감싼다.

    테이블·컬럼 이름은 코드가 정하는 값이라 사용자 입력이 아니지만, f-string으로 SQL을
    조립하는 습관 자체를 만들지 않는다.
    """
    delete = sql.SQL("delete from {} where {} = %s").format(
        sql.Identifier(table), sql.Identifier(date_column)
    )
    insert = sql.SQL("insert into {} ({}) values ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    return delete, insert


def replace_for_date(
    conn: psycopg.Connection,
    table: str,
    date_column: str,
    day: date,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> int:
    """
    commits, session_extracts 에서 수집한 원본 캐시에 적용. 커밋을 지우거나 그날 세션 내용이
    추가될 수 있으므로

    `day` 에 해당하는 행을 통째로 지우고 `rows` 로 다시 넣는다. 삽입 건수를 반환.

    **delete와 insert가 한 트랜잭션 안에 있어야 한다.** 중간에 죽으면 롤백되어
    이전 데이터가 그대로 남는다 — 지워진 채로 끝나는 상태가 존재하지 않는다.

    `rows` 가 비어도 delete는 실행한다. "그날 커밋이 하나도 없음"도 유효한 상태이고,
    이전 실행이 남긴 행을 지우지 않으면 재수집이 멱등이 아니게 된다.
    """
    delete, insert = replace_sql(table, date_column, columns)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(delete, (day,))
            if rows:
                cur.executemany(insert, rows)
    return len(rows)


def log_run(
    conn: psycopg.Connection,
    step: str,
    status: str,
    journal_date: date | None = None,
    detail: str | None = None,
) -> None:
    """처리 로그 한 줄 (NF-1, NF-3).
    log를 DB에 저장하는 로직.

    **`detail` 마스킹을 여기서 한다.** 호출부에 맡기면 반드시 빠뜨린다 — git 실패 메시지에는
    토큰이 박힌 원격 URL이 그대로 들어온다(`https://ghp_...@github.com`). 마스킹을 통과하는
    유일한 입구로 만들어 두면 잊을 방법이 없다 (D25).

    로그 기록 실패가 본 작업을 죽이면 안 되므로 호출부가 예외를 감싸서 쓴다.
    """
    if step not in STEPS:
        raise ValueError(f"알 수 없는 step: {step!r} (허용: {sorted(STEPS)})")
    if status not in LOG_STATUSES:
        raise ValueError(f"알 수 없는 status: {status!r} (허용: {sorted(LOG_STATUSES)})")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "insert into run_logs (journal_date, step, status, detail) values (%s, %s, %s, %s)",
                (journal_date, step, status, mask(detail)),
            )


def fetch_journal_status(conn: psycopg.Connection) -> list[tuple[date, str]]:
    """`journals` 의 (날짜, 상태) 전부. `dates.candidate_dates()` 의 입력이다.
    저널 테이블에서 날짜, 상태 맵핑 정보를 받아 list로 반환해주는 함수.

    **where 절이나 limit을 붙이면 안 된다.** 몇 달 전 `failed` 날짜가 조회에서 빠지면
    `scan_from` 이 그 날짜를 못 보고, D16 버그가 SQL 층에서 그대로 되살아난다.
    하루 한 행이라 10년을 돌려도 3,650행이다.
    """
    with conn.cursor() as cur:
        cur.execute("select journal_date, status from journals")
        return cur.fetchall()
