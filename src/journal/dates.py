"""후보 날짜 집합 계산 (§2.1, D16) — 파이프라인의 진입점.

journals 테이블 상태를 보고 "오늘 어떤 날짜들을 처리해야 하는가" 를 계산한다.
파이프라인의 진입점 — 여기서 나온 리스트가 수집·요약·저장의 루프 대상이 된다.

처리 대상은 **구간이 아니라 집합**이다. "마지막 종결 일지 다음 날 ~ 오늘" 로 짜면
사이에 낀 `failed` 날짜가 영구히 누락된다:

    7/1 failed, 7/2 done, 7/3 done  →  구간 방식은 7/4부터 시작, 7/1은 영영 재시도 안 됨

LLM 호출이 하루치만 실패하는 건 흔한 일(rate limit, 일시 오류)이라 이 구멍은 실제로 생긴다.
집합으로 정의하면 날짜 순서에 대한 의존이 사라지고 NF-1의 재시도 약속이 실제로 성립한다.

**행이 없는 날짜가 실무에서는 더 많다.** 없는 행은 SQL로 조회할 수 없으므로,
`scan_from` 부터 오늘까지의 달력을 코드로 만든 뒤 종결된 날짜를 빼는 방식으로 구한다.

전부 순수 함수다 — DB 없이 테스트된다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping

#: 종결 상태. 이 둘만 재처리 대상에서 빠진다.
#: 새 status 값을 추가할 때는 반드시 여기 편입 여부를 정해야 한다 (§5, §8).
#: 모르는 값은 **미종결로 취급**한다 — 조용히 누락되는 것보다 한 번 더 처리하는 쪽이 안전하다.
TERMINAL: frozenset[str] = frozenset({"done", "skipped"})


def is_terminal(status: str | None) -> bool:
    """종결 상태인가 확인하는 함수. 행이 없거나(None) 모르는 값이면 False(미종결).

    >>> is_terminal("done"), is_terminal("skipped")
    (True, True)
    >>> is_terminal("provisional"), is_terminal("failed"), is_terminal(None)
    (False, False, False)
    """
    return status in TERMINAL


def date_range(start: date, end: date) -> list[date]:
    """`start` 부터 `end` 까지 (양끝 포함). start > end 면 빈 리스트.
        인자로 받은 두 날짜 를 범위로한 달력 생성.

    >>> date_range(date(2026, 7, 1), date(2026, 7, 3))
    [datetime.date(2026, 7, 1), datetime.date(2026, 7, 2), datetime.date(2026, 7, 3)]
    >>> date_range(date(2026, 7, 3), date(2026, 7, 1))
    []
    """
    if start > end:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def scan_from(journal_status: Mapping[date, str], bootstrap_start: date) -> date:
    """달력을 어느 날짜부터 만들지 결정한다.
        날짜 상태 맵핑 정보를 인자로 받고, 호출부가 DB 테이블 조회 후 인자를 넘겨준다.

    `min(bootstrap_start, 가장 오래된 미종결 날짜, 마지막 종결 날짜 + 1일)`

    `min` 을 쓰는 이유: 사이에 낀 실패 날짜가 마지막 성공 날짜보다 **앞**일 수 있다.
    `max` 나 `last_terminal + 1` 단독으로 하면 그 날짜를 영영 못 본다 (D16).

    >>> rows = {date(2026, 7, 1): "failed", date(2026, 7, 2): "done"}
    >>> scan_from(rows, date(2026, 7, 19))
    datetime.date(2026, 7, 1)
    >>> scan_from({}, date(2026, 7, 19))
    datetime.date(2026, 7, 19)

    `bootstrap_start` 는 **생성 시작 하한**이다. 값을 과거로 내리면 그 지점부터 다시
    훑는다 — 단 done/skipped 는 종결이라 후보에서 빠지므로, 실제로 추가되는 건 아직
    일지가 없는 날짜와 failed 뿐이다. 이미 만든 일지를 다시 만들려면 여전히 해당
    `journals` 행을 지워야 한다.
    """
    opens = [d for d, s in journal_status.items() if not is_terminal(s)]
    terminals = [d for d, s in journal_status.items() if is_terminal(s)]

    starts: list[date] = [bootstrap_start]
    if opens:
        starts.append(min(opens))
    if terminals:
        starts.append(max(terminals) + timedelta(days=1))
    # 셋 중 가장 이른 날짜부터 훑는다.
    return min(starts)


def candidate_dates(
    journal_rows: Iterable[tuple[date, str]],
    today: date,
    bootstrap_start: date,
) -> list[date]:
    """처리해야 할 날짜 **전부**를 오래된 순으로 돌려준다.

    후보 = `scan_from` ~ `today` 중 `journals` 에 행이 없거나 미종결인 날짜.

    >>> rows = [(date(2026, 7, 1), "failed"),
    ...         (date(2026, 7, 2), "done"),
    ...         (date(2026, 7, 3), "skipped")]
    >>> candidate_dates(rows, today=date(2026, 7, 5), bootstrap_start=date(2026, 7, 1))
    [datetime.date(2026, 7, 1), datetime.date(2026, 7, 4), datetime.date(2026, 7, 5)]

    7/1 은 `failed` 라 재시도 대상, 7/2·7/3 은 종결이라 제외, 7/4·7/5 는 행이 없어 미처리.

    `--max-days` 절단은 여기서 하지 않는다. 호출부가 `take()` 로 자른다 —
    전체 후보를 알아야 "N일 남음" 을 안내할 수 있기 때문이다.
    """
    status = dict(journal_rows)
    ## 위 세 함수를 조합해서 일지 작성할 날짜만 들어있는 리스트를 반환
    # scan_from = 달력 생성 시작 날짜, is_terminal = 그 날짜가 done 인지 아닌지. date_range = 해당 범위에 달력 생성
    return [d for d in date_range(scan_from(status, bootstrap_start), today) if not is_terminal(status.get(d))]


def take(dates: list[date], max_days: int | None) -> tuple[list[date], int]:
    """앞에서 `max_days` 개만 취하고, 남은 개수를 함께 돌려준다.

    `max_days` 가 None 이거나 0 이하면 전부 처리한다.

    >>> ds = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
    >>> take(ds, 2)
    ([datetime.date(2026, 7, 1), datetime.date(2026, 7, 2)], 1)
    >>> take(ds, None)
    ([datetime.date(2026, 7, 1), datetime.date(2026, 7, 2), datetime.date(2026, 7, 3)], 0)

    일지 작성 후보 list 에서 max 앞에서 부터 max_days 개만 취한 list 반환
    잘려도 다음 실행이 같은 집합을 다시 계산하므로 남은 날짜가 이어서 처리된다 (D18).
    """
    if max_days is None or max_days <= 0:
        return list(dates), 0
    return list(dates[:max_days]), max(0, len(dates) - max_days)


def session_mtime_floor(candidates: list[date], margin_days: int) -> date | None:
    """세션 파일 mtime 컷 기준일 (F1-2). 후보가 없으면 None.
    후보 list 에 따라 클로드 세션 파일을 몇일지 까지 접근할지 정하는 로직.
    한 세션당 하루만 사용한다는 전제가 있으면 상관없지만, 한 세션에서 몇일을 작업 한다면 그 안에 내용을 날짜별로 파싱
    해야함. 따라서 오늘 일지를 쓰기 위해 몇일전 세션까지 접근 해볼지를 정해주어야함.응

    **후보 집합의 최소 날짜**에서 파생되어야 한다 — 마지막 종결 날짜가 아니다.
    오래된 실패 날짜가 후보에 있으면 스캔 범위도 그만큼 넓어져야 그 날짜의 세션을 다시 읽는다.

    mtime 은 파일 안의 최대 메시지 타임스탬프보다 항상 크거나 같으므로, 마진을 둔 컷은
    후보 날짜의 메시지를 놓치지 않는다. 이 필터는 순수 성능 최적화다.

    >>> session_mtime_floor([date(2026, 7, 10), date(2026, 7, 12)], margin_days=2)
    datetime.date(2026, 7, 8)
    >>> session_mtime_floor([], margin_days=2) is None
    True
    """
    if not candidates:
        return None
    return min(candidates) - timedelta(days=margin_days)
