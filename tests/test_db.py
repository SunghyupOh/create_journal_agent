"""db.py 중 DB 없이 검증 가능한 부분 — SQL 조립과 값 검증.

실제 연결·트랜잭션 원자성은 4단계 체크리스트에서 수동으로 확인한다
(중간에 죽였을 때 이전 데이터가 남아 있는가).
"""

import pytest

from journal.db import LOG_STATUSES, STEPS, replace_sql


def _text(composed) -> str:
    return composed.as_string(None)


def test_replace_sql_quotes_identifiers():
    delete, insert = replace_sql("commits", "commit_date", ["repo_path", "commit_hash"])
    assert _text(delete) == 'delete from "commits" where "commit_date" = %s'
    assert _text(insert) == 'insert into "commits" ("repo_path", "commit_hash") values (%s, %s)'


def test_replace_sql_placeholder_count_matches_columns():
    _, insert = replace_sql("t", "d", ["a", "b", "c", "d", "e"])
    assert _text(insert).count("%s") == 5


def test_step_and_status_match_db_check_constraint():
    """0001_init.sql 의 check 제약과 일치해야 한다. 한쪽만 고치면 삽입이 런타임에 실패한다."""
    assert STEPS == {"collect_git", "collect_sessions", "summarize", "write"}
    assert LOG_STATUSES == {"ok", "retry", "failed", "warn"}


@pytest.mark.parametrize("bad", ["collect", "COLLECT_GIT", "", "summarise"])
def test_unknown_step_rejected_before_db(bad):
    from journal.db import log_run

    with pytest.raises(ValueError, match="step"):
        log_run(conn=None, step=bad, status="ok")


@pytest.mark.parametrize("bad", ["success", "OK", "error"])
def test_unknown_log_status_rejected_before_db(bad):
    from journal.db import log_run

    with pytest.raises(ValueError, match="status"):
        log_run(conn=None, step="summarize", status=bad)
