# dev-journal

로컬 git 커밋 + 클로드 코드 대화를 수집해 사실 기반 개발 일지를 자동 생성하는 파이프라인.

## 응답 스타일

**한국어로 답하되, 꼭 필요한 내용만 최소한으로.**

- 서론·요약·재확인 생략. 결론부터.
- 이미 합의된 내용을 다시 설명하지 않는다.
- 표·목록은 정보가 실제로 구조적일 때만. 장식용 금지.
- 코드를 고쳤으면 무엇을 왜 고쳤는지 한두 줄. 전체 재설명 금지.
- 선택지를 나열하지 말고 추천안 하나를 제시한다. 이견이 있으면 그때 말한다.
- 다만 **설계가 깨지는 지점, 증상이 안 드러나는 버그, 보안 문제는 짧게라도 반드시 짚는다.**

## 용어

현업에서 쓰는 말 그대로 익히는 게 목적이다.

1. **정착된 한국어가 있으면 그걸 쓴다** — 멱등성, 정규화, 트랜잭션, 캐시, 마이그레이션, 의존성
2. **없으면 영어 원어 + 첫 등장 때만 괄호로 한 줄 설명.** 두 번째부터는 영어만.
   - `silent failure` (에러 없이 결과만 틀리는 것), `longest match wins`, `prune`, `guard clause`
3. **지어낸 번역어는 쓰지 않는다.** 한국 개발자들은 번역어 대신 영어 단어를 그대로 쓴다.

기존 스펙 문서(`dev-journal-spec-v2.5.md`)는 이 규칙 이전에 작성됐다. **표현만 고치려고 수정하지 않는다.**

## 현재 상태

- 스펙: `dev-journal-spec-v2.5.md` — 유일한 현행 문서. 이전 버전은 삭제됨.
- 구현: 환경 구성 완료. §7-C 1단계(`paths.py`)부터.
- 검증 완료: transcript 스키마(§7-A), 요약 품질 1차(§7-B).

## 핵심 설계 (스펙 §9 결정 기록 참조)

- **워크플로우형 파이프라인.** LLM은 요약 단계에만. 자율 루프 없음.
- **판단은 LLM, 포맷팅은 코드.** LLM은 JSON만 반환 → `write.py`가 마크다운 렌더링.
- **수집은 결정적 조회, 저장은 날짜별 REPLACE.** 멱등성이 정합성을 보장한다.
- **처리 대상은 미종결 날짜 "집합"** (구간 아님). 종결은 `done`/`skipped` 둘뿐. (D16)
- **DB에 들어가는 모든 텍스트는 마스킹을 거친다.** (D25)
- 단일 환경 전제 — 여러 기기에서 돌리면 날짜별 REPLACE가 서로의 커밋을 지운다. (D12/D24)

## 스택

Python 3.11+ / `anthropic` / `pydantic` / `psycopg[binary]` / Supabase(Postgres) / git CLI

- LLM: `claude-sonnet-5`, 비스트리밍 단발, `max_tokens=16000`, `effort=medium`
- structured output은 `messages.parse()` + Pydantic. `tool_choice` 강제 안 씀. (D23)
- DB 접근은 `psycopg` 직접 연결 — PostgREST는 delete+insert 트랜잭션 경계를 못 잡음
- 마이그레이션은 순서형 SQL 파일 (`supabase/migrations/0001_*.sql`)

## Silent failure 나기 쉬운 자리 (작업 시 주의)

- **경로 정규화** — transcript는 Windows UNC(`\\wsl.localhost\Ubuntu\...`), git은 POSIX + 저장소
  상대경로. `paths.py`를 안 거치면 프로젝트 그룹핑이 전부 "기타"로 떨어지고 파일 겹침 신호가
  하나도 안 맞는다. **에러가 아니라 품질 저하로 나타난다.** (D22)
- **후보 날짜 계산** — 구간으로 짜면 사이에 낀 `failed` 날짜가 영구히 누락된다. (D16)
- **`run_logs.detail` 마스킹** — git 실패 메시지에 토큰 박힌 원격 URL이 들어올 수 있다.
- **`isSidechain`** — 없는(`None`) 엔트리가 많으므로 `is True`로 비교.
- **도구 출력이 user 엔트리로 위장** — `origin.kind == "human"` 으로 걸러야 한다. (§7-A)
- **과다 병합** — sources 검증으로 잡히지 않는 유일한 실패 모드. 무관한 두 작업이 하나로 합쳐지면
  복구 불가.

## 파일

```
dev-journal-spec-v2.5.md    현행 스펙
src/journal/                구현 (§6 구조)
supabase/migrations/        순서형 마이그레이션
tests/                      paths·dates·mask는 순수 함수라 DB 없이 테스트
```
