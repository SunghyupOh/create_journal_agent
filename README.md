# dev-journal

https://github.com/user-attachments/assets/e2755a38-8797-4dd9-9ef4-6716a711bb70



로컬 git 커밋 + 클로드 코드 세션 대화를 수집해 **사실 기반 개발 일지를 자동 생성**하는 파이프라인.

하루치 작업 기록을 모아 LLM(Claude)으로 요약하고, 마크다운 일지로 렌더링한다.

```
   git 커밋─┐
            ├→ 수집(마스킹) → DB 캐시 → 프로젝트별 그룹핑 → LLM 요약 → 마크다운 일지
클로드 세션 ─┘
```

## 생성 결과 예시

```markdown
# 2026-07-21 개발 일지

## 작업 내용
- **[dev-journal]** get_full_diff LLM tool 추가 — DiffTool 구현, call_llm을 tool 루프로 변경
  - 출처: commit:ede5043, session:05f243d6

## 트러블슈팅
### [dev-journal] journal run 후 journals 테이블이 계속 비어 있음
- **원인**: psycopg 비-autocommit 기본값 → transaction()이 SAVEPOINT로 동작 → 전체 롤백
- **해결**: connect() 에서 conn.autocommit = True 설정

## 미해결
- config.JOURNAL_DIR 경로 오타 — 수정 여부 미결정
```

## 핵심 설계

- **워크플로우형 파이프라인** — 실행 순서·저장은 코드가 결정, LLM은 요약 판단에만 관여
- **판단은 LLM, 포맷팅은 코드** — LLM은 구조화된 JSON만 반환, 마크다운은 코드가 렌더링
- **멱등성** — 수집은 결정적 조회, 저장은 날짜별 REPLACE. 몇 번을 다시 돌려도 결과가 같다
- **처리 대상은 미종결 날짜 "집합"** — 사이에 낀 실패 날짜가 영구 누락되지 않는다
- **DB에 들어가는 모든 텍스트는 마스킹** — API 키·토큰·URL 자격증명 패턴 자동 제거
- **제한된 에이전트** — LLM이 잘린 diff를 스스로 조회하는 읽기 전용 tool 루프
  (자세한 내용: [ai-agent.md](ai-agent.md))

## 스택

Python 3.11+ · [anthropic](https://pypi.org/project/anthropic/) (Claude Sonnet, structured output) · pydantic · psycopg · Supabase(Postgres) · git CLI

## 설치

```bash
git clone https://github.com/SunghyupOh/create_journal_agent.git dev-journal
cd dev-journal
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 환경 설정

1. `.env.example`을 `.env`로 복사하고 채운다:
   - `ANTHROPIC_API_KEY` — Anthropic 콘솔에서 발급
   - `SUPABASE_DB_URL` — Supabase 대시보드 → Connection string (pooler 주소)
2. Supabase SQL Editor에서 `supabase/migrations/0001_init.sql` 실행 (테이블 6개 생성)
3. `src/journal/config.py`에서 환경에 맞게 수정:
   - `SCAN_ROOTS` — git 저장소를 찾을 루트 디렉터리들
   - `AUTHOR_EMAILS` — 수집할 커밋의 author 이메일
   - `SESSIONS_DIR` — 클로드 코드 transcript 위치
   - `JOURNAL_DIR` — 일지 마크다운 출력 위치
   - `BOOTSTRAP_START` — 일지 생성 시작 하한 날짜

## 사용

```bash
journal run               # 미처리 날짜 전부 처리
journal run --max-days 1  # 오래된 것부터 1일치만
```

- 이미 완료(`done`/`skipped`)된 날짜는 건너뛴다. 실패한 날짜는 다음 실행에서 자동 재시도.
- 오늘 날짜는 `provisional`로 저장되어 다음 실행에서 재요약된다 (하루가 끝나지 않았으므로).
- 비용: 하루치 요약 1회 ≈ $0.05 (Claude Sonnet 5 기준)

## 구조

```
src/journal/
├── config.py            설정
├── paths.py             경로 정규화 (Windows UNC ↔ POSIX)
├── dates.py             처리 후보 날짜 집합 계산
├── mask.py              비밀 마스킹 (토큰·URL 자격증명·KEY=value)
├── db.py                psycopg 연결, 날짜별 REPLACE, 처리 로그
├── collect_git.py       저장소 탐색 + git log 수집 (diff 3단 상한)
├── collect_sessions.py  클로드 코드 JSONL 파싱 (KST 날짜 분할)
├── summarize.py         그룹핑 → LLM 호출 (tool 루프) → journals 저장
├── prompts.py           시스템 프롬프트
├── schema.py            LLM 출력 Pydantic 스키마
├── tools.py             LLM 도구 (get_full_diff)
├── write.py             JSON → 마크다운 렌더링
└── main.py              journal CLI
supabase/migrations/     순서형 SQL 마이그레이션
tests/                   순수 함수 테스트 163건 (DB·LLM 불필요)
```

## 테스트

```bash
python -m pytest tests/ -q
```

수집·요약 로직의 순수 함수 부분만 검증한다 — DB 연결이나 LLM 호출 없이 돈다.

## 문서

- [dev-journal-spec-v2.5.md](dev-journal-spec-v2.5.md) — 기능 스펙 (설계 결정 기록 포함)
- [ai-agent.md](ai-agent.md) — AI 에이전트 요소 정리 (tool use, 프롬프트 캐싱, 설계 원칙)

## 주의

- **단일 환경 전제** — 여러 기기에서 동시에 돌리면 날짜별 REPLACE가 서로의 데이터를 지운다.
- `.env`는 절대 커밋하지 않는다 (`.gitignore` 처리됨).
