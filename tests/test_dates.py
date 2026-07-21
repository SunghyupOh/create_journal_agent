"""§7 후보 날짜 집합 테스트 케이스."""

from datetime import date

import pytest

from journal.dates import (
    TERMINAL,
    candidate_dates,
    date_range,
    is_terminal,
    scan_from,
    session_mtime_floor,
    take,
)

D = date  # 짧게


# --- 종결 상태 분류 ---------------------------------------------------------


def test_terminal_set_is_exactly_two():
    """종결은 done/skipped 둘뿐. 늘어나면 §2.1 분류를 함께 고쳐야 한다."""
    assert TERMINAL == {"done", "skipped"}


@pytest.mark.parametrize(
    "status, expected",
    [
        ("done", True),
        ("skipped", True),
        ("provisional", False),
        ("failed", False),
        (None, False),  # 행 없음
        ("무언가_새로운_상태", False),  # 모르는 값은 미종결로 — 조용한 누락 방지
    ],
)
def test_is_terminal(status, expected):
    assert is_terminal(status) is expected


# --- date_range -------------------------------------------------------------


def test_date_range_inclusive():
    assert date_range(D(2026, 7, 1), D(2026, 7, 3)) == [D(2026, 7, 1), D(2026, 7, 2), D(2026, 7, 3)]


def test_date_range_single_day():
    assert date_range(D(2026, 7, 1), D(2026, 7, 1)) == [D(2026, 7, 1)]


def test_date_range_reversed_is_empty():
    assert date_range(D(2026, 7, 3), D(2026, 7, 1)) == []


def test_date_range_crosses_month():
    assert date_range(D(2026, 7, 30), D(2026, 8, 2)) == [
        D(2026, 7, 30),
        D(2026, 7, 31),
        D(2026, 8, 1),
        D(2026, 8, 2),
    ]


# --- scan_from --------------------------------------------------------------


def test_scan_from_empty_uses_bootstrap():
    """첫 실행 — journals 가 비어 있으면 BOOTSTRAP_START."""
    assert scan_from({}, D(2026, 7, 19)) == D(2026, 7, 19)


def test_scan_from_all_terminal_starts_after_last():
    rows = {D(2026, 7, 1): "done", D(2026, 7, 2): "skipped"}
    assert scan_from(rows, D(2026, 7, 19)) == D(2026, 7, 3)


def test_scan_from_open_before_terminal_wins():
    """사이에 낀 failed 가 마지막 성공보다 앞이면 거기서 시작해야 한다 (D16 핵심)."""
    rows = {D(2026, 7, 1): "failed", D(2026, 7, 2): "done", D(2026, 7, 3): "done"}
    assert scan_from(rows, D(2026, 7, 19)) == D(2026, 7, 1)


def test_scan_from_all_open():
    rows = {D(2026, 7, 5): "provisional", D(2026, 7, 7): "failed"}
    assert scan_from(rows, D(2026, 7, 19)) == D(2026, 7, 5)


def test_scan_from_bootstrap_is_lower_bound():
    """BOOTSTRAP_START 를 과거로 내리면 거기부터 다시 훑는다 (생성 시작 하한).

    단 done/skipped 는 종결이라 후보에서 빠진다 — 과거를 넓혀도 이미 만든 일지는
    재생성되지 않고, 빈 날짜·failed 만 추가된다.
    """
    rows = {D(2026, 7, 19): "done"}
    assert scan_from(rows, D(2026, 7, 1)) == D(2026, 7, 1)
    got = candidate_dates(list(rows.items()), today=D(2026, 7, 20), bootstrap_start=D(2026, 7, 17))
    assert got == [D(2026, 7, 17), D(2026, 7, 18), D(2026, 7, 20)]  # 7/19(done)만 빠짐


# --- candidate_dates --------------------------------------------------------


def test_candidates_first_run():
    """첫 실행 → BOOTSTRAP_START ~ 오늘 전부."""
    got = candidate_dates([], today=D(2026, 7, 21), bootstrap_start=D(2026, 7, 19))
    assert got == [D(2026, 7, 19), D(2026, 7, 20), D(2026, 7, 21)]


def test_candidates_includes_sandwiched_failed():
    """★ v2.2 설계에서 누락됐던 케이스 — 7/1 failed 가 반드시 포함되어야 한다."""
    rows = [(D(2026, 7, 1), "failed"), (D(2026, 7, 2), "done"), (D(2026, 7, 3), "done")]
    got = candidate_dates(rows, today=D(2026, 7, 3), bootstrap_start=D(2026, 7, 1))
    assert got == [D(2026, 7, 1)]


def test_candidates_provisional_reprocessed_every_run():
    rows = [(D(2026, 7, 19), "done"), (D(2026, 7, 20), "provisional")]
    got = candidate_dates(rows, today=D(2026, 7, 20), bootstrap_start=D(2026, 7, 19))
    assert got == [D(2026, 7, 20)]


def test_candidates_skipped_is_terminal():
    rows = [(D(2026, 7, 19), "skipped")]
    got = candidate_dates(rows, today=D(2026, 7, 19), bootstrap_start=D(2026, 7, 19))
    assert got == []


def test_candidates_missing_rows_are_included():
    """행이 없는 날짜 — 실무에서 가장 많은 경우."""
    rows = [(D(2026, 7, 19), "done")]
    got = candidate_dates(rows, today=D(2026, 7, 22), bootstrap_start=D(2026, 7, 19))
    assert got == [D(2026, 7, 20), D(2026, 7, 21), D(2026, 7, 22)]


def test_candidates_mixed_realistic():
    """실패 하나 + 종결 몇 개 + 미처리 며칠 + 오늘 잠정."""
    rows = [
        (D(2026, 7, 15), "done"),
        (D(2026, 7, 16), "failed"),
        (D(2026, 7, 17), "done"),
        (D(2026, 7, 18), "skipped"),
        (D(2026, 7, 21), "provisional"),
    ]
    got = candidate_dates(rows, today=D(2026, 7, 21), bootstrap_start=D(2026, 7, 15))
    assert got == [D(2026, 7, 16), D(2026, 7, 19), D(2026, 7, 20), D(2026, 7, 21)]


def test_candidates_sorted_ascending():
    rows = [(D(2026, 7, 3), "failed"), (D(2026, 7, 1), "failed")]
    got = candidate_dates(rows, today=D(2026, 7, 3), bootstrap_start=D(2026, 7, 1))
    assert got == sorted(got)


def test_candidates_nothing_to_do():
    rows = [(D(2026, 7, 19), "done"), (D(2026, 7, 20), "done")]
    got = candidate_dates(rows, today=D(2026, 7, 20), bootstrap_start=D(2026, 7, 19))
    assert got == []


def test_candidates_accepts_any_iterable():
    rows = ((D(2026, 7, 19), "done"),)  # 튜플도 OK (DB 커서 결과 형태)
    assert candidate_dates(rows, today=D(2026, 7, 19), bootstrap_start=D(2026, 7, 19)) == []


# --- take (--max-days) ------------------------------------------------------


def test_take_truncates_oldest_first():
    ds = date_range(D(2026, 7, 1), D(2026, 7, 5))
    got, remaining = take(ds, 2)
    assert got == [D(2026, 7, 1), D(2026, 7, 2)]
    assert remaining == 3


def test_take_no_limit():
    ds = date_range(D(2026, 7, 1), D(2026, 7, 3))
    assert take(ds, None) == (ds, 0)
    assert take(ds, 0) == (ds, 0)


def test_take_limit_larger_than_list():
    ds = [D(2026, 7, 1)]
    assert take(ds, 10) == (ds, 0)


def test_take_resumes_next_run():
    """잘린 나머지가 다음 실행에서 이어진다 (D18)."""
    all_dates = date_range(D(2026, 7, 1), D(2026, 7, 5))

    first, remaining = take(all_dates, 2)
    assert remaining == 3

    # 처리한 날짜가 done 이 되었다고 가정하고 다시 계산
    rows = [(d, "done") for d in first]
    next_candidates = candidate_dates(rows, today=D(2026, 7, 5), bootstrap_start=D(2026, 7, 1))
    second, remaining2 = take(next_candidates, 2)

    assert second == [D(2026, 7, 3), D(2026, 7, 4)]
    assert remaining2 == 1


# --- session_mtime_floor ----------------------------------------------------


def test_mtime_floor_derives_from_oldest_candidate():
    """마지막 종결 날짜가 아니라 후보 집합의 최소 날짜에서 파생되어야 한다."""
    candidates = [D(2026, 7, 1), D(2026, 7, 20), D(2026, 7, 21)]
    assert session_mtime_floor(candidates, margin_days=2) == D(2026, 6, 29)


def test_mtime_floor_none_when_no_candidates():
    assert session_mtime_floor([], margin_days=2) is None
