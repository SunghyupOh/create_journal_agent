# 개발 일지 자동 생성 에이전트 — 프로젝트 스펙 v2.5

> 내 개발 활동(로컬 git 커밋 + 클로드 코드 대화)을 수집해서, 주관적 평가 없이 사실 기반으로
> "작업 내용 / 트러블슈팅 / 미해결" 구조의 일지를 기록한다.

> **v2.4 → v2.5 주요 변경** — 저장소를 SQLite → Supabase(Postgres)로 교체
> ① **DB를 Supabase로** (D24). 스키마를 Postgres 타입으로 번역, 날짜별 REPLACE를 트랜잭션으로 원자화
> ② **마스킹을 수집 경계로 앞당김** (D25, D7 수정). 원본이 외부 DB로 나가므로
>    **"DB에 들어가는 모든 텍스트는 마스킹을 거친다"** 는 단일 규칙으로 바꾼다
> ③ **NF-4 수정** — "전 과정 로컬"이 아니라 "외부 전송은 LLM API + Supabase, 단 전송 전 마스킹"
> ④ **접근 방식은 `psycopg` 직접 연결** — PostgREST(supabase-py)로는 delete+insert 트랜잭션 경계를
>    잡을 수 없어 날짜별 통째 교체가 원자적이지 않다
> ⑤ 마이그레이션을 **순서형 SQL 파일**로 관리 (`supabase/migrations/0001_*.sql`)
> ⑥ `mask.py` 구현 순서가 **collector보다 앞으로** 이동 (§7-C)
>
> 이전 변경 이력: v2.2(로컬 git 전환·워터마크 폐기), v2.3(후보 날짜 집합화·지문에 버전 포함·
> max-days·diff 3단 상한), v2.4(경로 정규화 모듈·structured output 교체·transcript 실측 반영).
> 상세 근거는 §9 설계 결정 기록 참조.

## 1. 배경 및 설계 원칙

- **에이전트가 아닌 워크플로우형 파이프라인.** 처리 순서가 항상 동일(수집 → 추출 → 요약 → 저장)하므로
  LLM이 다음 행동을 결정하는 자율 루프는 쓰지 않는다. LLM은 "요약" 단계에만 투입한다.
- **판단은 LLM, 포맷팅은 코드.** LLM은 구조화된 JSON만 반환하고, 마크다운 렌더링·"없음" 표기 등은
  코드가 처리한다. **JSON은 사람이 보는 산출물이 아니라 중간 표현이다** — 사람이 읽는 건 마크다운.
  이 분리가 있어야 ⓐ sources 코드 검증(F2-3), ⓑ 회고 단계의 항목 단위 취합(F4), ⓒ 일관된 렌더링,
  ⓓ 프롬프트 개선 후 LLM 재호출 없는 재렌더링(F3-3)이 전부 가능하다.
- **수집은 로컬에서, 저장은 Supabase에.** git·transcript는 전부 로컬 파일에서 읽는다.
  외부로 나가는 것은 LLM API 호출과 Supabase 저장 두 가지뿐이다.
- **DB에 들어가는 모든 텍스트는 마스킹을 거친다.** (D25) 원본이 외부 DB에 놓이므로, 마스킹 경계를
  Summarizer 입력이 아니라 **수집 경계**로 앞당긴다. 검증하기 쉬운 단일 규칙이다.
- **수집은 결정적 조회, 저장은 REPLACE.** 두 소스 모두 "특정 날짜로 질의 → 결과를 통째로 교체"라는
  동일한 모델이다. **멱등성이 정합성을 보장한다.**
- **처리 대상은 "미완료 날짜 집합"이지 "마지막 이후 구간"이 아니다.** 어떤 날짜가 실패하거나 잠정
  상태로 남으면, 그 뒤 날짜가 아무리 성공해도 계속 후보로 남는다. (D16)
- **경로는 한 곳에서 정규화한다.** 세션 transcript는 Windows UNC, git은 POSIX와 저장소 상대경로를
  준다. 정규화를 각자 하면 소스 간 연결이 조용히 깨진다. (D22)
- **수동 실행(on-demand).** 정기 스케줄 없음.
- **과거 날짜는 확정, 오늘은 잠정.** 오늘 일지는 재실행 시 통째로 재생성되고, 날짜가 지난 뒤 첫
  실행에서 확정된다.
- **원본 보존.** 요약 전 원본(마스킹된)을 DB에 남겨, 프롬프트 개선 후 재생성이 가능하도록 한다.
- **에이전트 프레임워크(LangChain 등) 사용 안 함.** 일반 Python 코드로 구현한다.

## 2. 기능 요구사항

### F1. 데이터 수집

| ID | 요구사항 |
|----|----------|
| F1-1 | **로컬 git 커밋 수집**: config의 스캔 루트 아래 `.git` 저장소를 자동 발견(`realpath` 정규화 후 중복 제거)하고, 대상 날짜별로 `git log --all --no-merges --author=<내 이메일들>` 조회 → 커밋 해시·메시지·변경 파일 목록·diff(3단 상한). **저장 직전 `mask.py` 통과**. 해당 날짜 행 전체를 한 트랜잭션 안에서 삭제 후 삽입(REPLACE) |
| F1-2 | **세션 파일 탐지**: `~/.claude/projects/` 아래 JSONL 중 **후보 날짜 집합의 최소 날짜 − 마진(기본 2일)** 이후 mtime인 파일을 탐지. **`subagents/` 하위 디렉터리는 통째로 제외**. 이 필터는 순수 성능 최적화 — 정합성은 F1-3이 보장 |
| F1-3 | **세션 추출은 결정적 재생성**: 탐지된 파일 전체를 재파싱해 §7-A의 확정 규칙대로 ⓐ 사람이 친 메시지 + 클로드 텍스트 응답, ⓑ Edit/Write의 **수정 파일 경로만**, ⓒ 엔트리의 `cwd`, ⓓ `gitBranch` 를 추출하고, 모든 경로를 `paths.py`로 정규화, **텍스트는 `mask.py` 통과** 후, 메시지 타임스탬프를 Asia/Seoul로 변환한 **날짜별로 그룹핑**하여 `(session_id, extract_date)` 행을 트랜잭션 안에서 REPLACE |
| F1-4 | CLI로 한 줄 메모 추가 (`journal note "..."`). 귀속 날짜는 실행 시점의 Asia/Seoul 날짜, `--date`로 덮어쓰기. **저장 전 마스킹** |
| F1-5 | *(제외)* PR·이슈 수집 — 로컬 git에 없는 정보 |

### F2. 일지 생성

| ID | 요구사항 |
|----|----------|
| F2-1 | `journal run` 실행 시 **후보 날짜 집합**(§2.1)의 각 날짜에 대해 커밋·세션 데이터를 조회하고, 데이터가 있으면 LLM으로 요약. 과거 날짜는 **확정(done)**, 오늘은 **잠정(provisional)**. 재생성 전 **입력 지문** 비교 — 같으면 LLM 재호출 없이 승격/유지. 고정 구조: ① 작업 내용 ② 트러블슈팅(문제 → 원인 → 해결) ③ 미해결 / 다음 할 일 |
| F2-2 | **Summarizer 입력은 프로젝트 단위로 구조화**한다. 정규화된 세션 cwd의 최근접 상위 저장소 루트를 프로젝트 키로 삼아 묶는다. 어느 저장소에도 속하지 않으면 "기타" 버킷 |
| F2-3 | 사실 기반 원칙: 입력에 근거 있는 내용만, 평가·과장 금지, 각 항목에 출처 표기. **같은 작업이 커밋과 세션 양쪽에 나타나면 항목 하나로 병합하고 출처를 둘 다 표기**. **한 작업을 두 섹션에 중복 기재 금지**(D21). **출처는 코드가 검증**: `sources` 값을 그 날짜에 수집된 커밋 해시/세션 ID 집합과 대조, 불일치 항목은 드롭하고 run_logs에 기록 |
| F2-4 | 활동 없는 과거 날짜는 마크다운 생략하되 `journals`에 `status='skipped'` 행을 남긴다. 오늘은 데이터가 없으면 아무것도 만들지 않는다 |
| F2-5 | 입력이 토큰 한도 초과 시 **프로젝트 단위 부분 요약 → 통합 요약** (map-reduce) |
| F2-6 | **마스킹은 수집 경계에서**(D25). DB에 저장되는 모든 텍스트 — 커밋 메시지·diff, 세션 추출 텍스트, CLI 메모, `run_logs.detail` — 가 대상. **불변 조건: 일지 마크다운은 LLM 출력에서만 렌더링한다** — 이제 DB 자체가 깨끗하므로 이 조건은 단일 방어선이 아니라 심층 방어가 된다 |

### F3. 저장 및 전달

| ID | 요구사항 |
|----|----------|
| F3-1 | 마크다운 파일 생성 (`journal/YYYY-MM-DD.md`) + 일지 전용 **private** git 레포에 자동 커밋 (커밋 전략은 §8) |
| F3-2 | 생성 완료 시 슬랙/디스코드로 요약 전송 |
| F3-3 | 요약 전 원본 데이터(마스킹된)를 DB 보관 — 프롬프트 개선 후 재생성 가능하도록 |

### F4. 회고 생성

| ID | 요구사항 |
|----|----------|
| F4-1 | 일일 일지들을 재요약해 주간/월간 단위 주요 작업·성과 압축 |
| F4-2 | 일일 일지의 트러블슈팅 항목들을 취합·선별·정리 (이력서 원재료) |

### NF. 비기능 요구사항

| ID | 요구사항 |
|----|----------|
| NF-1 | LLM 호출 실패 시 재시도 최대 3회(`Anthropic(max_retries=3)` — SDK가 429·5xx·연결 오류를 지수 백오프로 자동 재시도), 최종 실패 시 해당 날짜를 `failed`로 남긴다 — 종결 상태가 아니므로 **날짜 순서와 무관하게** 다음 실행의 후보 집합에 포함되어 재시도된다 |
| NF-2 | 하루 LLM 비용 상한 (v1.1). **단, 한 실행당 처리 날짜 상한(`--max-days`)은 MVP** (D18) |
| NF-3 | 처리 로그 기록. `run_date`뿐 아니라 처리 대상 `journal_date`도 남긴다. **`detail`도 마스킹 대상** |
| NF-4 | **수집은 전부 로컬 파일에서. 외부로 나가는 것은 LLM API 호출과 Supabase 저장 두 가지뿐이고, 둘 다 마스킹된 데이터만 나간다** (D25로 수정) |
| NF-5 | **DB 자격증명은 `.env`로만.** Supabase는 **직접 Postgres 연결 문자열**(또는 `service_role`)을 쓰므로 RLS를 우회한다 — `anon` 키를 쓰지 않는다. `.env`는 반드시 gitignore |

### 2.1 후보 날짜 집합 정의 (파이프라인의 진입점)

`journal run` 이 처리할 날짜는 **구간이 아니라 집합**이다.

```
oldest_open := journals에서 status ∉ {done, skipped} 인 행의 최소 journal_date (없으면 NULL)

scan_from   := min(oldest_open, last_terminal_date + 1일)
               단 journals가 비어 있으면 config.BOOTSTRAP_START
               (last_terminal_date = status ∈ {done, skipped} 인 행의 최대 journal_date)

candidates  := [scan_from … 오늘] 중
               journals에 행이 없거나 status ∈ {provisional, failed} 인 모든 날짜

처리 순서   := 오래된 날짜부터 오름차순
처리 개수   := 앞에서 최대 config.MAX_DAYS_PER_RUN 개 (--max-days로 덮어쓰기)
```

- **종결 상태는 `done` / `skipped` 둘뿐이다.** `provisional`·`failed`·행 없음은 전부 재처리 대상.
- **행이 없는 날짜가 실무에서는 더 많다** — 없는 행은 SQL로 조회할 수 없으므로, 코드가 `scan_from`부터
  오늘까지의 달력을 만든 뒤 `journals`에 있는 종결 날짜를 빼는 방식으로 구한다.
- `min()`을 쓰는 이유: 사이에 낀 실패 날짜가 마지막 성공 날짜보다 앞일 수 있다.
- F1-2의 세션 mtime 필터는 **`candidates`의 최소 날짜**에서 파생된다.

### MVP 범위

| 단계 | 포함 기능 | 목표 |
|------|-----------|------|
| **MVP** | F1-1~3, F2-1~6, F3-1, NF-1, NF-3~5, `--max-days` | 실행 한 번에 일지가 쌓인다 |
| v1.1 | F1-4(메모), F3-2(알림), NF-2(비용 상한) | 편의·운영 보강 |
| v1.2 | F3-3(재생성 기능), F4(회고), 필요 시 PR·이슈 보조 수집 | 이력서 소재 생산 |

MVP 판정 기준: **"언제 실행하든, 며칠을 건너뛰었든, 중간에 실패한 날이 있든, `journal run` 한 번에
밀린 일지가 전부 정확하게 생성되는가."** 2주 사용 후 v1.1 진행.

## 3. 아키텍처

```
사용자 실행: journal run [--max-days N]
        │
   [후보 날짜 집합 계산]  §2.1 — 미종결 날짜 전부 (구간 아님)
        │            ← Supabase journals 조회
        │
   [Collector]  ← 두 소스 모두 "날짜로 결정적 조회 → 마스킹 → 트랜잭션 REPLACE"
        ├─ 로컬 git: .git 발견(realpath 정규화) → 날짜별 git log
        │            → mask() → BEGIN; DELETE date; INSERT; COMMIT
        └─ 세션: 후보 최소 날짜 − 마진 이후 mtime인 JSONL (subagents/ 제외)
                 → 전체 재파싱 → 경로 정규화 → mask() → 날짜별 트랜잭션 REPLACE
        │
   [Summarizer]  ← LLM은 여기만
        │   ① 그 날짜의 commits + extracts 로드 → 프로젝트별 그룹핑
        │   ② 입력 지문 = hash(입력 ‖ prompt_version ‖ model ‖ schema_version)
        │      기존 일지와 같으면 LLM 호출 생략 (승격/유지만)
        │   ③ LLM 호출 — messages.parse() + Pydantic (크면 map-reduce 2단)
        │      ※ 마스킹은 이미 수집 단계에서 끝났다 (D25)
        │   ④ sources 검증 (수집된 해시/세션 ID 집합과 대조)
        │   ⑤ 과거 날짜 → done 확정 / 오늘 → provisional
        │
   [Writer]  JSON → 마크다운 렌더링 → git commit → (v1.1: 알림)
```

### 데이터 흐름 요약

- **로컬 git → 결정적 조회:** "D일에 author된 내 커밋"은 아무 때나 물어도 같은 답이 나온다.
  push 시점과 무관하므로 **"늦은 push" 문제 자체가 존재하지 않는다.** diff는 로컬 연산이라 무료.
- **클로드 코드 → mtime 스캔(pull):** 훅 없음. mtime은 파일 내 최대 메시지 타임스탬프 이상이므로,
  마진을 둔 mtime 컷은 후보 날짜의 메시지를 놓치지 않는다.
- **진행 중 세션도 안전:** 다음 실행이 파일 전체를 재파싱해 REPLACE한다. 자정을 걸친 세션은
  "D일까지의 메시지는 D일 일지에, 이후는 D+1 일지에" 자연스럽게 나뉜다.
- **프로젝트를 걸친 세션도 분리:** `(session_id, extract_date)` 행의 `project_dir`은 해당 날짜 구간
  엔트리의 최빈 cwd, 서로 다른 cwd가 섞이면 `cwd_mixed` 플래그. (D19 — 실측 확인됨)
- **두 소스의 연결:** 정확한 조인 키는 없다. 대신 ⓐ **프로젝트 경로**(저장소 루트 ↔ 세션 cwd),
  ⓑ **파일 경로 겹침**(커밋 변경 파일 ↔ 세션 Edit/Write 경로), ⓒ **브랜치 이름**이라는 신호를
  입력 구조에 담아 LLM이 의미로 연결하게 한다. **ⓐ·ⓑ는 `paths.py` 정규화를 거쳐야 성립한다**(D22).
  코드로 결정적 매칭기를 만들지 않는다 — 같은 파일을 건드린 무관한 작업을 오결합할 위험.

## 4. LLM 호출 설계

- **모델:** `claude-sonnet-5` (config). 더 나은 품질이 필요하면 `claude-opus-4-8`.
  **모델 ID는 지문에 포함되므로 바꾸면 잠정 일지가 자동으로 재생성된다.**
- **호출 형태:** 비스트리밍 단발 호출. `max_tokens=16000`
  (비스트리밍은 그 이상에서 SDK HTTP 타임아웃 위험. 출력이 JSON 한 덩어리라 충분).
- **thinking / effort:** `thinking={"type": "adaptive"}`, `output_config={"effort": "medium"}`.
  사실 추출·구조화 태스크라 깊은 추론이 필요 없다. 품질이 아쉬우면 `high`.
- **프롬프트 핵심 제약:**
  1. "입력에 근거가 있는 내용만 서술, 각 항목에 출처 표기."
  2. "같은 작업이 커밋과 세션 양쪽에 나타나면 **항목 하나로 합치고 sources에 둘 다** 넣어라."
  3. "이 대화/커밋에서 **실제로 수행한 작업만**. 이전에 이미 되어 있던 것에 대한 언급은 제외."
  4. "없으면 빈 배열을 반환하라" (표기는 렌더링 코드의 몫)
  5. **"한 작업을 두 섹션에 중복해서 쓰지 마라.** 트러블슈팅으로 해결한 내용은 work에 다시 쓰지 않는다." (D21)
  6. **"`pending`에는 명시적으로 미해결이라 언급됐거나 결정이 나지 않은 열린 질문만.
     기록에서 완료가 확인되지 않는다는 이유만으로 넣지 마라"** (`~ 완료 여부 미확인` 류 금지)
  7. "확신이 없으면 두 작업을 합치지 말고 나누어 둬라." (과다 병합 방지)
  8. "work는 기능·목적 단위로 묶는다. 더 큰 작업의 일부인 것을 독립 항목으로 만들지 않는다."
  9. "입력의 `***MASKED***` 는 비밀 값이 가려진 자리다. 그 값을 추측하거나 언급하지 마라."

### structured output — `messages.parse()` + Pydantic

**출력 스키마를 도구로 정의하고 `tool_choice`로 강제하지 않는다.** SDK의 structured
outputs(`output_config.format`)를 쓰고, Python에서는 `messages.parse()`에 Pydantic 모델을 넘긴다.
스키마 준수를 API가 보장하고 검증된 객체가 바로 돌아온다 — 파싱 코드가 없다. (D23)

```python
from pydantic import BaseModel
from typing import List

class WorkItem(BaseModel):
    desc: str
    project: str
    sources: List[str]

class TroubleItem(BaseModel):
    problem: str
    cause: str
    solution: str
    project: str
    sources: List[str]

class PendingItem(BaseModel):
    desc: str
    sources: List[str]

class JournalSummary(BaseModel):
    work: List[WorkItem]
    troubleshooting: List[TroubleItem]
    pending: List[PendingItem]

response = client.messages.parse(
    model=config.MODEL,                       # "claude-sonnet-5"
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "medium"},
    system=prompts.SYSTEM,
    messages=[{"role": "user", "content": grouped_input}],
    output_format=JournalSummary,
)
summary = response.parsed_output               # 검증된 JournalSummary 인스턴스
```

주의: structured outputs의 JSON Schema는 **재귀 스키마·수치 제약·문자열 길이 제약을 지원하지 않는다.**
현 스키마는 전부 평평한 리스트라 해당 없음. `stop_reason` 이 `refusal` 또는 `max_tokens` 이면
스키마 준수가 보장되지 않으므로 실패로 처리한다.

### 입력 지문 (input_fingerprint)

```
fingerprint = sha256(
    canonical_json(그 날짜의 commits + session_extracts + notes)
  ‖ PROMPT_VERSION ‖ MODEL ‖ OUTPUT_SCHEMA_VERSION
)
```

| 상황 | 전이 | 지문이 같으면 |
|---|---|---|
| 오늘 일지 재실행 | provisional → provisional | **유지** (LLM 생략) |
| 어제 일지가 provisional인 채 날짜가 지남 | provisional → done | **승격** (LLM 생략) |

두 번째가 매일 아침 반드시 발생한다 — 이게 없으면 매일 어제치를 공짜로 다시 요약하게 된다.
`failed` 날짜는 저장된 지문이 없어 항상 재호출한다. (D17)

### map-reduce 분할 규칙 (F2-5)

1. **1차: 프로젝트 단위.** 한 프로젝트의 커밋·세션이 같은 청크에 들어가므로 병합 지시(D14)가
   부분 요약 단계에서도 작동한다 — 분할 경계가 병합 경계를 깨지 않는다.
2. **2차: 단일 프로젝트 초과 시** — ⓐ diff를 먼저 버린다, ⓑ 그래도 넘으면 세션을 시간순 분할.
   **커밋 묶음은 쪼개지 않는다.**
3. 통합 요약에는 부분 요약 결과를 그대로 넣고 병합·중복 제거를 다시 지시한다.
   **sources는 부분 → 통합까지 전파**되어야 하며, 최종 검증은 통합 출력에 대해 수행한다.

### 출력 스키마 (JSON)

```json
{
  "work": [
    { "desc": "auth 미들웨어에 토큰 만료 검사 추가",
      "project": "dtartup-server",
      "sources": ["commit:a1b2c3", "session:xxx"] }
  ],
  "troubleshooting": [
    { "problem": "문제 상황", "cause": "원인", "solution": "해결 방법",
      "project": "dtartup-server", "sources": ["session:xxx"] }
  ],
  "pending": [
    { "desc": "미해결 항목 / 다음에 할 일", "sources": ["session:xxx"] }
  ]
}
```

### 검증되지 않는 실패 모드 — 과다 병합

sources 검증(F2-3)은 각 출처가 **실제로 수집된 값인지**만 본다. **묶음이 맞는지는 검증하지 못한다.**
같은 파일을 건드린 무관한 두 작업이 하나로 합쳐지면 sources는 전부 진짜 값이라 가드를 통과한다.

| 실패 방향 | 비용 | 검출 |
|---|---|---|
| 과다 분리 (하나를 둘로) | 낮음 — 일지가 장황해짐 | 사람이 읽으면 보임 |
| **과다 병합 (둘을 하나로)** | **높음 — 정보 소실, 복구 불가** | **코드로 불가** |

출력 스키마가 평평한 배열이라 **과다 포함(무관한 입력이 섞여 들어오는 것)의 비용은 사실상 0**이다 —
LLM이 항목 두 개로 나누면 그만이다. 그래서 D9(전부 합쳐 입력)가 성립한다. 위험한 건 반대 방향이고,
D14의 병합 지시가 그쪽으로 압력을 걸기 때문에 **프롬프트 7번(확신 없으면 분리)이 균형추다.**

## 5. 테이블 설계 (Supabase / Postgres)

마이그레이션은 **순서형 SQL 파일**로 관리한다 (`supabase/migrations/0001_*.sql` …).
아무것도 없는 상태에서 순서대로 실행하면 현 시점 스키마가 나오도록 각 파일을 작성한다.

```sql
-- supabase/migrations/0001_init.sql

-- 로컬 git 커밋 원본 (F1-1, 마스킹 후 저장)
create table commits (
    id             bigint generated always as identity primary key,
    commit_date    date        not null,   -- author date를 Asia/Seoul로 변환한 날짜
    repo_path      text        not null,   -- realpath 정규화된 저장소 루트 (프로젝트 키)
    repo_name      text        not null,   -- 표시용
    commit_hash    text        not null,   -- short hash (출처 표기용)
    author_email   text        not null,
    message        text        not null,   -- 마스킹됨
    files          text,                   -- 변경 파일 경로(저장소 상대), 개행 구분 — 연결 신호
    diff           text,                   -- 3단 상한 적용 + 마스킹됨
    diff_truncated boolean     not null default false,
    authored_at    timestamptz not null,
    unique (repo_path, commit_hash)
);
create index idx_commits_date on commits (commit_date);

-- 세션 메타 (F1-2)
create table sessions (
    session_id      text primary key,
    transcript_path text        not null,
    last_seen_mtime timestamptz             -- 스캔 진단용 (정합성에는 미사용)
);

-- 세션 추출 결과 — 날짜별 (F1-3, 결정적 재생성 대상)
create table session_extracts (
    id           bigint generated always as identity primary key,
    session_id   text    not null,
    extract_date date    not null,          -- 메시지 타임스탬프(UTC)를 Asia/Seoul로 변환
    project_dir  text,                      -- 정규화된 최빈 cwd — 프로젝트 그룹핑 키
    cwd_mixed    boolean not null default false,
    git_branch   text,                      -- transcript의 gitBranch — 보조 연결 신호
    text         text    not null,          -- 사람이 친 메시지 + 클로드 텍스트 응답 (마스킹됨)
    edited_files text,                      -- Edit/Write 대상 경로(정규화) 목록 — 연결 신호
    unique (session_id, extract_date)       -- REPLACE 단위
);
create index idx_extracts_date on session_extracts (extract_date);

-- CLI 메모 (F1-4, v1.1이지만 스키마는 미리 확정)
create table notes (
    id         bigint generated always as identity primary key,
    note_date  date        not null,
    text       text        not null,        -- 마스킹됨
    created_at timestamptz not null default now()
);
create index idx_notes_date on notes (note_date);

-- 생성된 일지 (F2, F3-1)
create table journals (
    id                bigint generated always as identity primary key,
    journal_date      date not null unique,
    summary_json      jsonb,                -- LLM 출력 원본 (skipped면 NULL)
    markdown_path     text,
    prompt_version    text,
    model             text,
    input_fingerprint text,
    journal_commit    text,                 -- 일지 레포 커밋 해시 (잠정 동안 amend되며 갱신)
    status            text not null default 'done'
        check (status in ('done', 'provisional', 'failed', 'skipped'))
);
create index idx_journals_status on journals (status);   -- 후보 집합 계산용

-- 처리 로그 (NF-1, NF-3)
create table run_logs (
    id           bigint generated always as identity primary key,
    run_date     timestamptz not null default now(),
    journal_date date,                      -- 처리 대상 날짜 (수집 단계 등은 NULL)
    step         text not null
        check (step in ('collect_git', 'collect_sessions', 'summarize', 'write')),
    status       text not null
        check (status in ('ok', 'retry', 'failed', 'warn')),
    detail       text,                      -- 마스킹됨
    created_at   timestamptz not null default now()
);
create index idx_run_logs_journal_date on run_logs (journal_date);
```

### SQLite → Postgres 번역 요점

| SQLite | Postgres | 비고 |
|---|---|---|
| `INTEGER PRIMARY KEY` | `bigint generated always as identity primary key` | |
| `TEXT` 날짜 (`'YYYY-MM-DD'`) | `date` | 진짜 타입을 쓰면 범위 조회·정렬이 정확해진다 |
| `TEXT` ISO8601 | `timestamptz` | |
| `INTEGER` 플래그 | `boolean` | |
| `TEXT DEFAULT (datetime('now'))` | `timestamptz not null default now()` | |
| `summary_json TEXT` | `jsonb` | 나중에 회고(F4)에서 항목 단위 조회가 쉬워진다 |
| (없음) | `check (status in (...))` | 잘못된 상태값이 들어오는 걸 DB가 막는다 |

### 날짜별 REPLACE는 트랜잭션으로

SQLite의 `INSERT OR REPLACE` 관용구를 그대로 옮기지 않는다. 날짜 단위 통째 교체는 **삭제 + 삽입**이고,
둘 사이에서 실패하면 그 날짜 데이터가 사라진다.

```sql
begin;
delete from commits where commit_date = %s;
insert into commits (...) values (...), (...), ...;
commit;
```

**이 때문에 접근 방식은 `psycopg` 직접 연결이다.** Supabase의 PostgREST(`supabase-py`)는
delete와 insert가 별개의 HTTP 요청이라 트랜잭션 경계를 잡을 수 없다. 실패해도 다음 실행이
재수집해 자가 치유되긴 하지만(멱등성 덕분), 원자성을 공짜로 얻을 수 있는데 포기할 이유가 없다.

연결 문자열은 Supabase 대시보드의 **Connection string → Session pooler** 값을 쓴다.

### 설계 의도

- **`state` 테이블 없음.** 두 소스 모두 날짜로 결정적 조회가 되므로 증분 수집 상태가 필요 없다.
- **`journals`가 진행 상태의 단일 원천이다.** 종결(`done`/`skipped`)과 미종결의 구분이 시스템 전체의
  유일한 상태 축이므로, **새 status 값을 추가할 때는 반드시 §2.1의 분류와 `check` 제약에 함께
  편입시켜야 한다.**
- `commits`와 `session_extracts`는 **원본의 파생 캐시**다. 날짜 단위 통째 교체이므로 멱등.
  DB가 통째로 날아가도 git 히스토리와 transcript 파일에서 재생성된다.
- `files` / `edited_files` / `git_branch` → 커밋과 세션을 잇는 **연결 신호**. 조인 키가 아니라 증거다.
- 일지↔데이터 간 FK 없음(의도) → 날짜 컬럼 조회로 충분, 관계를 고정하지 않는 게 단순하다.
- **RLS는 켜지 않는다.** 단일 사용자 백엔드가 직접 연결로 쓰는 DB다. 대신 `anon` 키를 절대 쓰지 않고
  연결 문자열을 `.env`로만 관리한다 (NF-5). 나중에 웹 UI를 붙이면 그때 RLS를 설계한다.

## 6. 패키지 구조

```
dev-journal/
├── pyproject.toml
├── .env                       # ANTHROPIC_API_KEY, SUPABASE_DB_URL  (gitignore 필수)
├── supabase/
│   └── migrations/
│       ├── 0001_init.sql
│       └── README.md          # 실행 순서·주의사항
├── src/journal/
│   ├── config.py              # 스캔 루트, 이메일, 부트스트랩, 타임존, 모델, 상한값들
│   ├── paths.py               # D22: UNC→POSIX, 절대→저장소 상대, realpath (순수 함수)
│   ├── dates.py               # §2.1 후보 날짜 집합 계산 (순수 함수)
│   ├── mask.py                # D25: 비밀 패턴 마스킹 — 수집 경계에서 호출 (순수 함수)
│   ├── db.py                  # psycopg 연결 풀 + 트랜잭션 헬퍼
│   ├── collect_git.py         # F1-1
│   ├── collect_sessions.py    # F1-2, F1-3
│   ├── schema.py              # §4의 Pydantic 출력 모델
│   ├── summarize.py           # F2: 그룹핑 → 지문 → LLM → sources 검증, map-reduce
│   ├── prompts.py             # 프롬프트 템플릿 + PROMPT_VERSION, OUTPUT_SCHEMA_VERSION
│   ├── write.py               # F3-1: JSON→마크다운, git commit
│   └── main.py                # 오케스트레이션 + CLI 엔트리포인트
└── tests/
    ├── test_paths.py          # UNC 변환, 저장소 상대경로 변환
    ├── test_dates.py          # 중간 failed, provisional, 첫 실행, max-days 절단
    ├── test_mask.py           # 각 비밀 패턴, 오탐(정상 문자열을 가리지 않는지)
    └── fixtures/              # 샘플 transcript, 샘플 git 저장소
```

구조 원칙: **파이프라인 단계 = 파일 하나.** 각 단계는 "DB에서 읽고 DB에 쓰는" 독립 함수라
단계별 개별 실행·테스트가 가능하고, **앞 단계만 만들고 멈춰도 그 자체로 돌아간다.**

`paths.py` · `dates.py` · `mask.py` 는 전부 순수 함수라 DB 없이 단위 테스트한다.
셋 다 틀리면 아래 단계가 조용히 틀리는 자리다.

### 설정 항목 (config.py)

| 항목 | 예시 | 비고 |
|------|------|------|
| `SUPABASE_DB_URL` | `.env`에서 로드 | Session pooler 연결 문자열. 코드에 하드코딩 금지 |
| `SCAN_ROOTS` | `["~/projects", "~/ai_mentor"]` | `.git` 자동 발견. **중첩 금지** |
| `AUTHOR_EMAILS` | `["jsmoon4738@gmail.com", "tjdguq8042@gmail.com"]` | **이름이 아닌 이메일로 필터** |
| `BOOTSTRAP_START` | `"2026-07-19"` | 첫 실행 시 어느 날짜부터 |
| `TIMEZONE` | `"Asia/Seoul"` | 모든 날짜 귀속 기준 |
| `WSL_UNC_PREFIX` | `r"\\wsl.localhost\Ubuntu"` | paths.py가 벗겨낼 접두사 |
| `DIFF_MAX_LINES_PER_FILE` | 200 | |
| `DIFF_MAX_LINES_PER_COMMIT` | 1000 | 파일 수 많은 커밋 방어 |
| `DIFF_MAX_LINES_PER_DATE` | 5000 | 하루 총량 |
| `MAX_DAYS_PER_RUN` | 7 | `--max-days`로 덮어쓰기 |
| `SESSION_MTIME_MARGIN_DAYS` | 2 | mtime 컷 여유 |
| `MODEL` | `"claude-sonnet-5"` | 지문에 포함됨 |
| `MAX_TOKENS` | 16000 | 비스트리밍 단발 호출 기준 |
| `EFFORT` | `"medium"` | `output_config.effort` |
| `JOURNAL_REPO` | `~/journal` | 일지 전용 private 레포 |

### 기술 스택

- Python 3.11+ / `anthropic` SDK / `pydantic` / `psycopg[binary]` / git CLI(`subprocess`)
- `python-dotenv` (`.env` 로드)
- **`supabase-py` 불필요** — 트랜잭션이 필요해 직접 Postgres 연결을 쓴다
- 실행: 수동 (`journal run`). 설계상 실행 시점·간격에 무관하므로 나중에 아무 스케줄러나 얹으면 된다.

## 7. 구현 순서

**§7-A(transcript 스키마)는 실측으로 확인 완료.** 남은 리스크는 요약 품질 하나다.

### 7-A. transcript 스키마 (실측 확인 완료 ✅)

`ai-mentor` 세션 1개(348 엔트리) 기준 실측:

```
    63  user:tool_result      ← 도구 출력. 버림
    63  assistant:tool_use    ← Edit/Write에서 file_path만 추출
    41  assistant:text        ← 뽑음
    30  assistant:thinking    ← 버림
    15  user:<str>            ← 뽑음 (사람이 친 메시지)
    30  file-history-snapshot ┐
    19  mode / permission-mode│
    19  ai-title              ├ 전부 노이즈, 버림
    18  last-prompt           │
    15  attachment / system   ┘
```

**확정된 추출 규칙:**

```python
def extract(entry):
    if entry.get("isSidechain") is True:      # None인 엔트리가 다수 → `is True`로 비교
        return
    t = entry.get("type")

    # 함정 1: 도구 출력이 user 엔트리로 위장한다 (user 79개 중 63개!)
    #   origin.kind == "human" 이 사람이 친 메시지를 정확히 골라낸다 (실측 15/15 일치)
    if t == "user" and entry.get("origin", {}).get("kind") == "human":
        yield ("text", entry["message"]["content"])

    elif t == "assistant":
        for b in entry["message"]["content"]:
            if b["type"] == "text" and b["text"].strip():      # thinking 자동 제외
                yield ("text", b["text"])
            elif b["type"] == "tool_use" and b["name"] in ("Edit", "Write"):
                yield ("file", normalize(b["input"]["file_path"]))   # content는 버림 (D11)
```

- **타임스탬프:** `"timestamp": "2026-06-23T13:47:29.439Z"` — **UTC(Z)**. 날짜별 분할 설계 성립.
- **`cwd`:** 모든 엔트리에 존재. **한 세션에서 2종 관측됨** → D19 실증 확인.
- **함정 2:** `isSidechain` 이 없는(`None`) 엔트리가 많으므로 `is True` 로 비교할 것.
- **보너스 신호:** `gitBranch` 필드 존재 → 연결 신호로 활용.
- **경로:** `cwd`·`file_path` 모두 **Windows UNC** (`\\wsl.localhost\Ubuntu\home\...`) → D22.

### 7-B. 요약 품질 (1차 검증 통과 ✅ / 프롬프트 튜닝 남음)

실제 세션(수정 파일 15개, Edit 24회, Bash 11회)으로 검증한 결과:

- ✅ **트러블슈팅 3건이 problem/cause/solution으로 정확히 추출됨.**
  `cause`가 재진술이 아닌 실제 진단으로 나옴 — **도구 출력을 전부 버렸는데도** 클로드의 텍스트 응답이
  진단을 서술하고 있기 때문. **D11 확정, 예비안(에러 출력 일부 살리기) 폐기.**
- ✅ 입력에 없는 내용을 지어내지 않음. 활동 없는 세션에서 `[]` 반환.
- ⚠️ `pending`에 "~ 완료 여부 미확인" 류가 4건 중 3건 → 프롬프트 6번으로 대응
- ⚠️ work ↔ troubleshooting 중복 1건 → D21 / 프롬프트 5번으로 대응
- ⚠️ work 입도가 잚(11개) → 프롬프트 8번. 단 **커밋이 함께 들어가면 자연히 개선될 가능성**이 있어
  (커밋 메시지가 작업 경계 신호) 실제 데이터로 재확인 후 판단

### 7-C. 구현 순서 (이해 우선)

각 단계가 끝날 때마다 실행해서 눈으로 볼 수 있게, 미완성 조각을 둘 이상 들고 있지 않게 배치했다.

| # | 단계 | 끝나면 아는 것 / 확인 방법 |
|---|------|------|
| 1 | `paths.py` + 테스트 | 경로 정규화. 의존성 0, 순수 함수 |
| 2 | `dates.py` + 테스트 | **이 프로그램의 상태 모델 전체.** 뭐가 끝났고 뭐가 안 끝났는지, 왜 실패해도 안전한지 |
| 3 | `mask.py` + 테스트 | 비밀 패턴 마스킹. **collector보다 먼저** — 수집 경계에서 호출되므로(D25) |
| 4 | `0001_init.sql` + `db.py` | Supabase에 테이블 생성, psycopg 연결·트랜잭션 헬퍼. 대시보드에서 테이블 확인 |
| 5 | `collect_git.py` | 날짜별 트랜잭션 REPLACE 패턴을 **쉬운 데이터로** 먼저 익힘. `select * from commits` |
| 6 | `collect_sessions.py` | 같은 패턴 + JSONL 파싱(§7-A 규칙). 여기가 제일 오래 걸림 |
| 7 | `schema.py` + `summarize.py` | 커밋+대화가 어떻게 한 덩어리로 LLM에 들어가는지. **sources 검증·map-reduce는 뺀다** |
| 8 | `write.py` | JSON → 마크다운. git 커밋은 아직 안 함 |
| 9 | `main.py` | **처음으로 `journal run`이 됨.** 전체 그림이 이 파일 한 장에 보임 |
| 10+ | 가드·편의 | sources 검증 → 일지 레포 커밋 → `--max-days` → map-reduce → 지문 → 메모/알림/회고 |

7단계에서 가드를 빼는 이유: 정상 경로가 돌아가기 전에 넣으면 "요약이 이상한 건지 가드가 이상한 건지"를
동시에 디버깅하게 된다. (마스킹은 이제 3단계로 앞당겨졌으므로 예외 — 수집 자체가 마스킹에 의존한다.)

### 테스트 케이스

**경로 정규화 (1단계)**
- [ ] `\\wsl.localhost\Ubuntu\home\oh\ai_mentor\backend` → `/home/oh/ai_mentor/backend`
- [ ] `/home/oh/ai_mentor/backend/llm/client.py` + repo `/home/oh/ai_mentor` → `backend/llm/client.py`
- [ ] 저장소 밖 경로 → 상대화하지 않고 절대경로 유지 ("기타" 버킷 행)
- [ ] 심볼릭 링크가 걸린 저장소 루트가 realpath로 동일 판정되는가

**후보 날짜 집합 (2단계)**
- [ ] 첫 실행(journals 비어 있음) → `BOOTSTRAP_START ~ 오늘`
- [ ] **중간에 낀 `failed`** — `7/1 failed, 7/2 done, 7/3 done` → 후보에 **7/1이 포함**되는가
- [ ] 오늘이 `provisional` → 매 실행 재처리 대상
- [ ] `skipped`는 종결 → 재처리 안 됨
- [ ] `MAX_DAYS_PER_RUN` 절단 시 오래된 날짜부터, 남은 건 다음 실행에서 이어지는가
- [ ] 세션 mtime 컷이 **후보 집합의 최소 날짜**에서 파생되는가

**마스킹 (3단계)**
- [ ] `sk-ant-`, `sk-`, `ghp_`, `github_pat_`, `AKIA`, `AIza`, `xox[baprs]-`, JWT 각각 가려지는가
- [ ] **오탐 없는가** — 정상 코드·커밋 메시지의 긴 문자열(해시, base64 데이터)을 가리지 않는가
- [ ] `.env` 형태(`KEY=value`)의 값 부분을 어디까지 가릴지 정책 결정 후 테스트
- [ ] 마스킹은 되돌릴 수 없지만, **원본은 git·transcript에 그대로 남아 있어 재수집으로 복구된다**
      — 패턴을 고친 뒤 재수집하면 자동으로 반영된다 (D5의 결정적 재생성 덕분)

**DB (4단계)**
- [ ] 날짜별 REPLACE가 트랜잭션 안에서 원자적인가 (중간에 죽이면 이전 데이터가 남아 있는가)
- [ ] `check` 제약이 잘못된 status·step 값을 막는가

## 8. 구현 시 주의사항

### 마스킹 (D25 — 이제 수집 경계에 있다)

- **DB에 들어가는 모든 텍스트가 대상**: `commits.message`, `commits.diff`,
  `session_extracts.text`, `notes.text`, `run_logs.detail`
- **`run_logs.detail`을 빠뜨리기 쉽다** — git 명령 실패 메시지에 토큰이 박힌 원격 URL이 들어올 수 있다
- **마스킹은 되돌릴 수 없지만 안전하다.** 진짜 원본은 git 히스토리와 transcript 파일에 그대로 있고,
  수집은 결정적 재생성(D5)이므로 패턴을 고친 뒤 재수집하면 반영된다. **`mask.py`를 고쳤으면
  해당 날짜의 `journals` 행을 지워 재처리 대상으로 만들 것** (지문에 mask 버전이 안 들어가므로 자동 감지 안 됨)
- **오탐이 과탐보다 낫다** — 가려서 곤란한 건 일지 품질이 조금 나빠지는 정도지만, 못 가리면 외부 DB에 평문으로 남는다

### 경로 (D22 — 가장 조용히 깨지는 부분)

- **세션 쪽은 전부 Windows UNC**: `\\wsl.localhost\Ubuntu\home\ohsunghyup\ai_mentor\backend`
- **git 쪽은 POSIX + 저장소 상대**: `repo_path=/home/ohsunghyup/ai_mentor`, `files=backend/llm/client.py`
- 변환은 2단계: ① UNC → POSIX, ② 절대 → 저장소 상대
- **이게 없으면 F2-2의 프로젝트 그룹핑이 전부 "기타"로 떨어지고, 파일 겹침 신호는 한 건도 맞지 않는다.**
  증상이 에러가 아니라 "품질이 왜 이러지"로 나타나므로 특히 조심

### Supabase / DB

- **날짜별 REPLACE는 반드시 한 트랜잭션 안에서** (`begin; delete; insert; commit;`).
  `supabase-py`(PostgREST)로는 경계를 잡을 수 없으므로 `psycopg` 직접 연결을 쓴다
- **마이그레이션은 순서형 파일로.** 아무것도 없는 상태에서 `0001` → `0002` … 순서대로 실행하면
  현 시점 스키마가 나오도록 작성한다. 기존 테이블이 있는 상태에서 `create table if not exists` 를
  쓰면 **구조가 갱신되지 않은 채 다음 마이그레이션이 새 컬럼을 참조해 실패**한다 — 개발 중 스키마를
  갈아엎을 땐 관련 테이블을 drop한 뒤 처음부터 재실행하는 편이 확실하다
- **연결 문자열은 `.env`로만.** `anon` 키를 쓰지 않는다 (RLS 우회가 전제이므로)
- **대량 삽입은 `executemany`** 또는 `COPY`. 하루치라 성능 문제는 아니지만 왕복 횟수는 줄이는 게 낫다
- **네트워크 실패도 재시도 대상.** 수집 중 연결이 끊기면 그 날짜는 다음 실행에서 다시 수집된다
  (멱등성) — 별도 복구 로직이 필요 없다

### git 수집

- **`--all` 필수.** 기본 `git log`는 HEAD 기준이라 브랜치를 옮겨두면 누락된다
- **`--no-merges` 권장.** 머지 커밋은 일지 가치가 없다
- **author 필터는 이메일로.** 한 사람이 여러 이름 표기를 쓴다
  (`jooseong moon`/`JooseongMo0n` → `jsmoon4738@`, `ohsunghyup`/`sunghyupoh` → `tjdguq8042@`)
- **저장소 발견 시 `realpath` 정규화 + 중복 제거.** 스캔 루트가 중첩되면 같은 저장소가 두 경로로
  잡혀 커밋이 중복되는데, `unique (repo_path, commit_hash)`는 `repo_path`가 달라 막지 못한다
- **author date 기준 귀속.** committer date는 rebase 시 갱신된다. git의 `--since/--until` 타임존
  해석에 의존하지 말고 **후보 구간 ±1일로 넉넉히 뽑은 뒤 Python에서 Asia/Seoul 변환해 거를 것**
- **다른 기기 커밋은 수집되지 않는다.** 단일 환경 전제 (D12). Supabase를 쓰더라도 이 전제는 그대로다
  — 여러 기기에서 돌리면 날짜별 REPLACE가 서로의 커밋을 지운다. 필요해지면 REPLACE 단위를
  `(commit_date, repo_path)`로 좁혀야 한다

### 토큰·비용

- **transcript 원본은 큼** → 도구 출력 제거 필수. 실측: 348 엔트리 → 56 메시지
- **diff 상한은 3단** (파일당/커밋당/날짜당). 절삭 시 `diff_truncated=true` + run_logs `warn`,
  **프롬프트에도 "이 diff는 잘려 있음"을 명시**해 LLM이 없는 내용을 추측하지 않게 한다
- **`--max-days` 브레이크** — 부트스트랩을 과거로 잡으면 첫 실행이 무제한으로 때린다

### 그 외

- **`subagents/` 디렉터리 제외** — `isSidechain` 플래그와 별개로 서브에이전트 transcript가
  `~/.claude/projects/<proj>/<session>/subagents/agent-*.jsonl` 로 **별도 파일**로도 존재한다
- **"없음" 처리** — LLM에게 "없으면 없음이라 써라" 지시 금지. 빈 배열 → 코드가 렌더링
- **날짜 경계** — 모든 날짜 귀속은 UTC/로컬 → Asia/Seoul 변환 후. KST는 DST가 없어 단순
- **일지 레포 커밋 전략** — 날짜당 커밋 하나 유지. `provisional` 동안은 직전 커밋을 `--amend`
  (해시를 `journals.journal_commit`에 갱신), `done` 확정 시 최종 커밋. 단 **amend 대상이 그 날짜의
  일지 커밋이 맞는지 확인 후** 수행하고, 아니면 새 커밋을 쌓는다. 커밋 실패는 run_logs에 `failed`로
  남기되 **일지 생성 자체는 성공으로 처리** — 마크다운과 DB는 이미 있다
- **`skipped`는 종결 상태** — rebase로 과거 날짜에 커밋이 새로 생기는 경우는 v1.2 재생성으로 처리
- **새 status 값을 추가할 때는 §2.1의 분류와 DB `check` 제약에 함께 편입시킬 것**

## 9. 설계 결정 기록

| # | 결정 | 근거 |
|---|------|------|
| ~~D1~~ | ~~워터마크 수집~~ → **D12로 철회** | GitHub API 폴링 전제였다 |
| ~~D2~~ | ~~워터마크 갱신 시점~~ → **D12로 철회** | 위와 같음 |
| D3 | **SessionEnd 훅 제거**, mtime 스캔으로 대체 | 훅은 강제 종료·크래시 시 발동 보장이 없고, 실시간성이 필요 없는 시스템에 실시간 통지 도구를 쓸 이유가 없음 |
| D4 | project_dir은 **transcript의 cwd 필드**에서 | 디렉터리명 역산은 하이픈 포함 경로에서 모호 |
| D5 | 세션 추출은 **결정적 재생성 + REPLACE** | append는 부분 실패 시 중복(비멱등). 전체 재파싱 → 날짜별 통째 교체는 순수 함수라 항상 멱등. **부수 효과로 마스킹 패턴을 나중에 고쳐도 재수집으로 반영된다(D25)** |
| D6 | **과거=확정, 오늘=잠정 재생성** | "오늘 일지를 오늘 만들면" 생성 이후 활동이 갈 곳이 없다. 경계 문제를 재생성으로 흡수 |
| ~~D7~~ | ~~마스킹은 Summarizer 입력 경계 한 곳~~ → **D25로 이동** | 저장소가 외부(Supabase)로 나가면서 전제가 바뀌었다 |
| D8 | **sources 검증은 코드 가드** | 환각 방어를 일회성 점검이 아니라 상시 검증으로 |
| D9 | **커밋 기준 단일 소스안 기각** — 이중 소스 유지 | 커밋↔세션은 1:1이 아님. "미해결"은 정의상 커밋 없는 작업이라 커밋 기준으론 구조적으로 항상 빔. **출력이 평평한 배열이라 과다 포함의 비용이 0인 것이 이 결정을 뒷받침한다** |
| D10 | **정기 스케줄 제거, 수동 실행** | 정해진 시각 실행이 실제 요구가 아니었고, WSL cron 문제도 소멸 |
| D11 | 세션 추출에 **수정 파일 경로만, 코드 원문 제외** | 토큰. **실증: 도구 출력을 전부 버려도 클로드 텍스트 응답이 진단을 서술해 `cause`가 구체적으로 나왔다(§7-B)** — 예비안 폐기. 부수 효과로 `.env` 등의 내용 유출도 차단 |
| D12 | **GitHub API → 로컬 git 직접 조회** | 로컬 커밋이 항상 push에 선행. 토큰·rate limit 불필요, diff 무료, "늦은 push" 문제 소멸, **결정적 날짜 조회로 워터마크 전체가 불필요.** 대가: PR·이슈 제외, **다른 기기 커밋 누락(D24에서도 유지되는 전제)** |
| D13 | **Summarizer 입력을 프로젝트 단위로 구조화** | 정확한 조인 키가 없음. 저장소 경로↔cwd, 커밋 파일↔수정 파일, 브랜치 이름을 배치로 제공해 LLM이 의미로 연결하게 한다. 코드로 결정적 매칭기를 만들면 오결합 위험 |
| D14 | **`sources` 배열 + 병합 지시** | 한 작업이 양쪽에 증거를 남기는 게 정상. 단일 문자열이면 중복 계상된다 |
| D15 | **author 필터는 이메일** | 한 사람이 4가지 이름 표기, 이메일은 2개뿐 |
| D16 | **후보 날짜를 연속 구간 → 미종결 집합으로** | "마지막 종결 다음 날 ~ 오늘"은 **사이에 낀 `failed`를 영구히 건너뛴다**. NF-1의 재시도 약속과 정면 모순 |
| D17 | **입력 지문에 prompt_version·model·schema_version 포함** | 데이터 해시만으로는 프롬프트 개선 직후에도 옛 결과가 승격된다 |
| D18 | **`--max-days` 브레이크를 MVP로** | 부트스트랩을 과거로 잡으면 첫 실행이 무제한으로 때린다. **폭주를 멈추는 브레이크는 첫 실행 전에 있어야 한다** |
| D19 | **project_dir을 `sessions` → `session_extracts`로** | `cwd`는 엔트리별 필드. **실측: 한 세션에서 cwd 2종** |
| D20 | **diff 상한 3단** | 파일당만 걸면 파일 80개 커밋이 16,000줄. 절삭 사실을 DB·로그·프롬프트에 모두 남긴다 |
| D21 | **섹션 간 중복 금지 (work ↔ troubleshooting)** | D14는 커밋↔세션 중복만 다뤘다. 실측에서 같은 작업이 양쪽에 나왔다. `pending` 정의도 좁힌다: **"기록에서 완료가 확인되지 않음"은 미해결이 아니다** |
| D22 | **경로 정규화 모듈 (`paths.py`)** | 실측 결과 transcript의 `cwd`·`file_path`가 전부 **Windows UNC**인 반면 git은 POSIX와 저장소 상대경로를 준다. 정규화 없이는 프로젝트 그룹핑이 전부 "기타"로 떨어지고 파일 겹침 신호가 한 건도 맞지 않는다 — **에러가 아니라 품질 저하로 나타나 조용히 깨진다** |
| D23 | **structured output: `tool_choice` 강제 → `messages.parse()` + Pydantic** | 도구로 위장해 강제하는 건 structured outputs가 없던 시절의 우회책. 현재 SDK는 `output_config.format`으로 스키마 준수를 API가 보장하고 검증된 객체를 돌려준다 — 도구 정의·파싱·검증 코드가 사라진다 |
| **D24** | **SQLite → Supabase (Postgres)** | 로컬 DB 파일 관리를 피하고 익숙한 스택을 쓴다. 부수 이득: 진짜 `date`/`timestamptz`/`jsonb`/`check` 타입, 트랜잭션으로 날짜별 REPLACE 원자화. 대가: NF-4 수정(외부 전송 발생), 오프라인 실행 불가, 자격증명 관리. **접근은 `psycopg` 직접 연결** — PostgREST는 delete+insert 트랜잭션 경계를 잡을 수 없다. **단일 환경 전제(D12)는 그대로** — 여러 기기에서 돌리면 날짜별 REPLACE가 서로의 커밋을 지운다 |
| **D25** | **마스킹을 Summarizer 입력 경계 → 수집 경계로** (D7 대체) | D7은 "일지는 LLM 출력에서만 렌더링되므로 입력만 마스킹하면 충분"이라는 전제였고, DB가 로컬 파일이라 원본 저장이 안전했다. **Supabase로 가면 마스킹 전 diff와 대화 전문이 외부 DB에 평문으로 남는다.** 규칙을 **"DB에 들어가는 모든 텍스트는 마스킹을 거친다"** 로 바꾸면 검증하기 쉽고, "마크다운은 LLM 출력에서만" 불변 조건은 단일 방어선이 아니라 심층 방어가 된다. 마스킹이 비가역이지만 **진짜 원본은 git·transcript에 남아 있고 수집이 결정적 재생성(D5)이라 패턴을 고친 뒤 재수집하면 반영된다** — 이 성질 덕분에 앞당기는 게 안전하다 |
