"""§7 마스킹 테스트 케이스."""

import pytest

from journal.mask import mask

# --- 각 패턴이 가려지는가 --------------------------------------------------

SECRETS = [
    ("anthropic", "sk-ant-api03-AbCdEf0123456789ghijKLMNOP_-qrstuvwxyz"),
    ("openai", "sk-proj-AbCdEf0123456789ghijKLMNOPqrstuvwxyz"),
    ("github", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"),
    ("github", "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"),
    ("aws", "AKIAIOSFODNN7EXAMPLE"),
    ("google", "AIzaSyA0123456789abcdefghijklmnopqrstuvw"),
    # 문자열을 쪼개서 씀 — GitHub push protection 이 진짜 토큰으로 오인해 push 를 막는다
    ("slack", "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ),
]


@pytest.mark.parametrize("kind, secret", SECRETS)
def test_each_pattern_is_masked(kind, secret):
    out = mask(f"앞부분 {secret} 뒷부분")
    assert secret not in out
    assert f"[MASKED:{kind}]" in out
    assert out.startswith("앞부분 ") and out.endswith(" 뒷부분")


@pytest.mark.parametrize("kind, secret", SECRETS)
def test_masking_is_idempotent(kind, secret):
    """재수집 시 두 번 거쳐도 결과가 같아야 한다."""
    once = mask(f"x {secret} y")
    assert mask(once) == once


# --- run_logs.detail: 토큰 박힌 원격 URL (가장 빠뜨리기 쉬운 자리) ----------


def test_git_remote_url_with_token():
    detail = (
        "fatal: unable to access "
        "'https://ghp_0123456789abcdefghijklmnopqrstuvwxyz@github.com/oh/x.git/': 403"
    )
    out = mask(detail)
    assert "ghp_" not in out
    assert "github.com/oh/x.git" in out  # 나머지는 진단에 필요하므로 남아야 한다


def test_token_in_url_is_masked_once():
    """토큰이 URL 사용자 자리에 있으면 URL 규칙이 그 위를 덮어써 표기가 깨질 수 있다."""
    out = mask("https://ghp_0123456789abcdefghijklmnopqrstuvwxyz@github.com/x.git")
    assert out == "https://[MASKED:github]@github.com/x.git"
    assert mask(out) == out


def test_postgres_connection_string():
    out = mask("postgresql://postgres.abcd:s3cr3tpw@aws-0.pooler.supabase.com:5432/postgres")
    assert "s3cr3tpw" not in out
    assert "[MASKED:url]" in out
    assert "aws-0.pooler.supabase.com:5432/postgres" in out


@pytest.mark.parametrize(
    "password",
    [
        "s3cr3tpw",
        "&9eC/bCW@MqCfEw",  # `/` 와 `@` 포함 — 좁은 패턴이면 매칭이 끊겨 평문이 샌다
        "a@b@c",
        "pw/with/slashes",
        "p%40ssw0rd",  # 퍼센트 인코딩된 것
    ],
)
def test_password_with_special_chars_is_masked(password):
    out = mask(f"postgresql://postgres.abcd:{password}@db.xxx.supabase.co:5432/postgres")
    assert password not in out
    assert "db.xxx.supabase.co" in out  # 호스트는 진단에 필요하므로 남는다


def test_url_without_credentials_untouched():
    url = "https://github.com/oh/dev-journal/pull/3"
    assert mask(url) == url


def test_ssh_remote_untouched():
    """git@github.com 은 스킴이 없어 매칭되지 않는다 (이메일 오탐 방지)."""
    s = "git@github.com:oh/x.git 로 remote 변경"
    assert mask(s) == s


def test_bearer_header():
    out = mask("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnop" not in out
    assert "Bearer [MASKED:bearer]" in out


# --- KEY=value 정책 ---------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "DB_PASSWORD=hunter2ABC",
        "SUPABASE_SECRET=abcdef123456",
        "SLACK_TOKEN='abcdef123456'",
        'GITHUB_ACCESS_KEY="abcdef123456"',
        '{"api_key": "abcdef123456"}',
        "PRIVATE_KEY=abcdef123456",
    ],
)
def test_secret_assignment_is_masked(line):
    out = mask(line)
    assert "[MASKED:env]" in out
    assert "hunter2ABC" not in out and "abcdef123456" not in out


@pytest.mark.parametrize(
    "line",
    [
        "MAX_TOKENS=16000",  # 숫자 — 이 프로젝트에서 실제로 나온다
        "max_tokens: 16000",
        "PRIMARY_KEY=id",  # 맨 KEY 를 키워드에 넣지 않은 이유
        "AUTHOR=ohsunghyup",  # AUTH 를 키워드에 넣지 않은 이유
        "api_key=os.environ['ANTHROPIC_API_KEY']",  # 값이 아니라 참조
        "SUPABASE_DB_URL=${DB_URL}",
        "USE_TOKEN_CACHE=true",
        "ANTHROPIC_API_KEY=sk-ant-...",  # .env.example 자리표시자
    ],
)
def test_benign_assignment_untouched(line):
    assert mask(line) == line


# --- 오탐 없는가 ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "커밋 a1b2c3d4e5f67890abcdef1234567890abcdef12 되돌림",  # git SHA-1
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "base64 데이터 SGVsbG8gV29ybGQgdGhpcyBpcyBub3QgYSBzZWNyZXQ=",
        "def to_repo_relative(path: str, repo_root: str) -> str:",
        "paths.py 에서 is_within 이 문자열 접두사 대신 경로 경계로 비교한다",
        "https://docs.anthropic.com/en/api/messages 참고",
        "7/1 failed, 7/2 done — 사이에 낀 날짜가 누락되던 버그 수정",
    ],
)
def test_no_false_positive(text):
    assert mask(text) == text


# --- 경계 ------------------------------------------------------------------


def test_none_and_empty_pass_through():
    assert mask(None) is None
    assert mask("") == ""


def test_multiple_secrets_in_one_text():
    text = (
        "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf0123456789ghijKLMNOP\n"
        "remote: https://ghp_0123456789abcdefghijklmnopqrstuvwxyz@github.com/x.git\n"
        "정상 문장은 그대로 남는다"
    )
    out = mask(text)
    assert "sk-ant-" not in out and "ghp_" not in out
    assert "정상 문장은 그대로 남는다" in out


def test_diff_with_env_file():
    """diff 에 .env 가 섞여 들어오는 경우 — KEY=value 규칙을 넣은 이유."""
    diff = "+++ b/.env\n+DB_PASSWORD=s3cr3tpw\n+DEBUG=true"
    out = mask(diff)
    assert "s3cr3tpw" not in out
    assert "DEBUG=true" in out
