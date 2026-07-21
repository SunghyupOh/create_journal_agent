-- 0001_init.sql — 초기 스키마 (스펙 §5)
--
-- 실행: Supabase 대시보드 → SQL Editor 에 붙여넣거나
--       psql "$SUPABASE_DB_URL" -f supabase/migrations/0001_init.sql
--
-- commits / session_extracts 는 원본(git·transcript)의 파생 캐시다. 통째로 날아가도
-- 재수집으로 복원된다. 진짜 상태는 journals 하나뿐이다.

-- 로컬 git 커밋 원본 (F1-1, 마스킹 후 저장)
create table if not exists commits (
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
create index if not exists idx_commits_date on commits (commit_date);

-- 세션 메타 (F1-2)
create table if not exists sessions (
    session_id      text primary key,
    transcript_path text        not null,
    last_seen_mtime timestamptz             -- 스캔 진단용 (정합성에는 미사용)
);

-- 세션 추출 결과 — 날짜별 (F1-3, 결정적 재생성 대상)
create table if not exists session_extracts (
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
create index if not exists idx_extracts_date on session_extracts (extract_date);

-- CLI 메모 (F1-4, v1.1이지만 스키마는 미리 확정)
create table if not exists notes (
    id         bigint generated always as identity primary key,
    note_date  date        not null,
    text       text        not null,        -- 마스킹됨
    created_at timestamptz not null default now()
);
create index if not exists idx_notes_date on notes (note_date);

-- 생성된 일지 (F2, F3-1)
create table if not exists journals (
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
create index if not exists idx_journals_status on journals (status);   -- 후보 집합 계산용

-- 처리 로그 (NF-1, NF-3)
create table if not exists run_logs (
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
create index if not exists idx_run_logs_journal_date on run_logs (journal_date);
