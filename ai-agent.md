# AI 에이전트 요소 정리

기능 스펙(`dev-journal-spec-v2.5.md`)과 별개로, 이 프로그램의 **AI 에이전트 관련 요소**만 모은 문서.
구현 위치: `src/journal/tools.py`, `src/journal/summarize.py`, `src/journal/prompts.py`.

## 1. 전체 구도 — 워크플로우 + 제한된 에이전트

이 프로그램은 **워크플로우형 파이프라인**이다. 실행 순서(수집 → 요약 → 렌더링)와 저장은
전부 코드가 결정하고, LLM은 요약 단계 안에서만 돈다. 자율 루프는 없다 (§9).

에이전트 요소는 그 요약 단계 **내부**에 한정된다: LLM이 스스로 판단해서 도구를 호출할 수
있는 tool use 루프. 경계는 이렇다:

| | 담당 | 예 |
|---|---|---|
| 판단이 필요한 일 | LLM (+tool) | 잘린 diff를 더 볼지, 두 작업을 합칠지 |
| 판단이 필요 없는 일 | 코드 | 수집, 저장, 렌더링, 알림(예정) |

**쓰기 도구는 만들지 않는다.** 저장·렌더링을 코드가 담당해야 마스킹(D25)과 멱등성이 유지된다.
LLM에게 주는 도구는 전부 읽기 전용이다.

## 2. Tool use 구조

### 등록 — `tools.TOOL_SCHEMA`

도구는 `name` + `description` + `input_schema`(JSON Schema) dict로 정의해서 API 요청의
`tools` 인자로 넘긴다. **description이 호출 조건을 정한다** — "생략됨 표시가 있고 꼭 필요할
때만"을 명시해야 멀쩡한 diff까지 재조회하지 않는다.

### 루프 — `summarize.call_llm`

API는 무상태라 매 호출마다 대화 전체를 다시 보낸다. 루프 골격:

```
messages = [user 입력]
반복 (상한 _MAX_TURNS = 8):
    response = client.messages.parse(..., tools=[...], output_format=JournalSummary)
    stop_reason == "end_turn"  → parsed_output 반환 (정상 종료)
    stop_reason == "tool_use"  → assistant 응답을 messages에 붙이고,
                                  tool_use 블록 전부 실행 →
                                  tool_result 들을 한 user 메시지로 붙여 재호출
    그 외 (max_tokens/refusal) → 예외 → journals failed (D17)
```

- `stop_reason`은 API가 응답에 담아주는 필드다 — 모델이 왜 멈췄는지 서버가 판정한다.
- 한 응답에 `tool_use` 블록이 **여러 개** 올 수 있다 (parallel tool use). 전부 실행해서
  결과를 **한 user 메시지에 모아** 보낸다 — 나눠 보내면 모델이 병렬 호출을 안 하게 된다.
- `block.input`은 LLM이 만든 값이라 `**kwargs`로 풀지 않고 `.get()`으로 꺼낸다 —
  예상 밖 키로 TypeError가 나면 그날 요약이 통째로 죽는다.
- tool을 안 쓰는 날은 기존과 동일한 1회 호출로 끝난다.

### 도구 구현 — `tools.DiffTool`

diff 3단 상한(§8)으로 잘린 커밋의 전체 diff를 LLM이 직접 조회하는 도구. 설계 원칙:

1. **경로는 LLM에게 받지 않는다.** 그날 커밋의 `hash → repo_path` 맵을 코드가 만들어
   주입한다. LLM이 경로를 추측하면 없는 저장소를 뒤진다.
2. **반환 직전 `mask()`.** 상한 처리 전 원문이 API로 나가는 자리라 `log_run`과 같은
   choke point 원리 (D25).
3. **상한 2중.** 호출 5회 + 출력 2000줄. LLM이 조회를 반복하며 비용을 태우거나
   lockfile급 diff가 컨텍스트를 채우는 걸 코드가 막는다. 잘린 사실은 텍스트에 남긴다.
4. **실패는 예외 대신 문자열 반환.** tool_result로 돌려줘야 LLM이 읽고 대처한다.
   예외를 던지면 그날 요약 전체가 failed가 된다.
5. **stderr에 실시간 로그.** `[도구] get_full_diff ...` — 실행 중 LLM이 뭘 조회하는지
   보인다. stdout은 파이프라인 출력용이라 섞지 않는다. `flush=True` 필수.

## 3. Structured output (D23)

`client.messages.parse()` + `output_format=JournalSummary`(Pydantic). 스키마 준수를
API가 보장하므로 JSON 파싱 실패 처리가 없다. 단 보장은 `stop_reason == "end_turn"`일
때만 성립한다 — `max_tokens`로 잘리면 JSON이 중간에 끊겼을 수 있어 실패 처리한다.

프롬프트는 번호 붙은 명령형 규칙 목록(`prompts.SYSTEM`)이다. 실험으로 확인된 것:
**규칙 체계 밖의 대화체 지시는 무시되기 쉽다.** 도구 호출을 유도하려면 같은 격의
번호 규칙으로 넣어야 들었다.

## 4. 프롬프트 캐싱

캐시 저장·조회는 API 서버가 하지만 **opt-in이다** — 요청에 `cache_control` 마커를
찍어야 발동한다. 컨텍스트는 `tools → system → messages` 순서로 조립되고, 캐싱은
**프리픽스 매칭**이다: 마커 위치까지의 토큰 열을 해시해서, 다음 요청의 앞부분이
바이트 단위로 일치하면 그 구간의 연산을 재사용한다 (~10% 가격).

우리 적용: 첫 user 메시지 블록에 마커 1개 → tools + system + 하루치 입력 전체가
캐시 범위. 루프 2턴째부터 이 구간이 캐시로 처리된다.

손익 (하루치 입력 ~22k 토큰 기준):

| tool 호출 | 턴 수 | 캐시 없음 | 캐시 있음 |
|---|---|---|---|
| 0회 | 1 | 1.0배 | 1.25배 (유일한 손해, ~$0.01) |
| 1회 | 2 | 2.0배 | 1.35배 |
| 2회 | 3 | 3.0배 | 1.45배 |

쓰기 1.25배 / 읽기 0.1배 / 유지 5분. 손익분기는 요청 2번 = tool 1회부터 이득.

주의: `cache_control`은 모델에게 전달되는 내용이 아니라 서버용 메타데이터다.
날짜가 다르면 입력이 달라 날짜 간 캐싱은 의미 없다 — 효과는 루프 안에서만 난다.
**tools 목록을 바꾸면 프리픽스 맨 앞이 바뀌어 캐시 전체가 무효.**

적중 확인: `response.usage.cache_read_input_tokens` (0이면 프리픽스 어딘가가 매번 다름).

## 5. 비용 (측정값)

- 하루치 요약 1회(tool 미사용): 약 22k 입력 / 1k 출력 ≈ **$0.054** (Sonnet 5 intro 가격)
- tool 1회 사용 시: +재호출 1턴, 캐싱 덕에 ~$0.07 수준
- 모델: `claude-sonnet-5`, `thinking adaptive`, `effort medium`, 비스트리밍

## 6. 용어 구분 (혼동했던 것들)

- **tool use** — LLM에게 도구 스키마를 주고, LLM 판단으로 호출 → 코드가 실행 →
  결과를 돌려주는 구조. 지금 쓰는 방식.
- **MCP** — 외부 서비스들이 tool 정의+실행을 표준 형식으로 미리 만들어둔 프로토콜.
  "MCP를 쓴다" = LLM이 판단해서 호출한다는 뜻. 코드에서 외부 API를 그냥 부르는 건
  MCP가 아니다.
- **webhook** — 미리 등록한 URL로 이벤트 발생 시 HTTP 요청을 보내는 패턴. AI와 무관.
  Slack 알림(예정)은 판단이 필요 없는 결정적 후처리라 LLM tool이 아니라 코드로 구현한다.
  URL 자체가 비밀이므로 `.env`에 보관.

## 7. 향후 후보 / 넣지 않는 것

넣을 후보 (전부 읽기 전용):

1. **`get_recent_journals(n)`** — 최근 일지 조회. 미해결 연속성("어제 pending이 오늘
   해결됨")과 중복 억제가 가능해진다. journals 테이블 SELECT라 구현 비용 최소.
2. **tool 사용 내역을 `run_logs`에 기록** — 어떤 날 무슨 조회를 했는지 추적.
   `cache_read_input_tokens`도 같이 남기면 비용 추적 겸용.
3. **web search** — 요약 단계에는 넣지 않는다 (아래). 회고 기능(10+)에서 "그때 결론이
   정확했나" 검증용으로만 후보.
4. **`read_session_extract`** — map-reduce(10+) 도입 후에나 의미 있음. 보류.

넣지 않는 것:

- **쓰기 tool** (일지 수정, DB 저장, 알림 발송) — 판단은 LLM, 실행과 저장은 코드.
  이 경계가 설계의 뼈대다 (§9).
- **자율 루프** (LLM이 알아서 여러 날 처리) — 처리 대상 결정은 `dates.py`의 몫.
- **요약 단계의 web search** — 일지는 "그날 실제로 한 일"의 기록이라, 입력 밖 텍스트가
  스며들면 기록이 아니라 창작이 된다 (규칙 1과 충돌).
