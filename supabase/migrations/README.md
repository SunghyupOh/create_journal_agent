# 마이그레이션

번호 순서대로 **한 번씩** 실행한다. 이미 적용된 파일은 다시 실행하지 않는다.

## 실행

Supabase 대시보드 → SQL Editor 에 파일 내용을 붙여넣고 실행하거나:

```bash
psql "$SUPABASE_DB_URL" -f supabase/migrations/0001_init.sql
```

## 주의

- **연결 문자열은 Session pooler 값을 쓴다.** `anon` 키가 아니다 (D24, NF-5).
- `create table if not exists` 라 재실행해도 안전하지만, **컬럼 변경은 감지하지 못한다.**
  스키마를 바꿀 때는 `0001` 을 고치지 말고 `0002_*.sql` 을 새로 만든다.
- **RLS는 켜지 않는다.** 단일 사용자 백엔드가 직접 연결로 쓰는 DB다 (§5).

## 적용 이력

| 파일 | 내용 | 적용일 |
|---|---|---|
| `0001_init.sql` | 초기 6개 테이블 | (미적용) |
