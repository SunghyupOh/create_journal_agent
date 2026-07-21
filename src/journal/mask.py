"""비밀 문자열 마스킹 (D25) — 수집 경계에서 호출한다.

특정 패턴 문자열을 다른 문자열로 대체하는 모듈 (masking)

DB에 들어가는 **모든** 텍스트가 대상이다:
`commits.message`, `commits.diff`, `session_extracts.text`, `notes.text`, `run_logs.detail`.

`run_logs.detail` 을 빠뜨리기 쉽다 — git 실패 메시지에 토큰이 박힌 원격 URL이 그대로 들어온다.

되돌릴 수 없지만 안전하다. 진짜 원본은 git 히스토리와 transcript 파일에 그대로 있고,
패턴을 고친 뒤 재수집하면 자동으로 반영된다 (D5 결정적 재생성).

**오탐이 미탐보다 낫다** — 못 가리면 호스팅 DB에 평문으로 남지만, 잘못 가리면 일지 품질이
조금 나빠질 뿐이다. 다만 `MAX_TOKENS=16000` 처럼 실제로 자주 나오는 표현까지 가리면
일지가 못 읽을 물건이 되므로, 값이 명백히 비밀이 아닌 경우만 좁게 빼준다(`_looks_benign`).

전부 순수 함수다.
"""

from __future__ import annotations

import re

#: 마스킹 결과 표기. 이 문자열 자체는 어떤 패턴에도 걸리지 않아야 한다 (멱등성).
MASK = "[MASKED:{kind}]"


# --- ① 형태만으로 확신할 수 있는 토큰 -------------------------------------
#
# 발급처가 정한 접두사 + 길이가 있어 오탐 위험이 사실상 없다. 통째로 치환한다.
# `sk-ant-` 를 `sk-` 보다 먼저 둔다 — 앞의 것이 이기도록.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("google", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    # JWT — 세 마디 모두 base64url. `eyJ` 는 `{"` 의 base64라 헤더/페이로드 시작이 고정이다.
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]

#: `Authorization: Bearer xxx` — 접두사는 남기고 값만 가린다.
_BEARER = re.compile(r"(?P<pre>\bBearer\s+)(?P<val>[A-Za-z0-9_\-.=]{16,})", re.IGNORECASE)

#: URL 안의 자격증명. `postgresql://user:pw@host`, `https://token@github.com` 둘 다.
#: `git@github.com` 처럼 스킴이 없는 형태는 매칭하지 않는다(이메일·SSH 오탐 방지).
#:
#: **비밀번호는 탐욕적(greedy)으로 잡는다.** `[^\s/@]+` 로 좁히면 `/` 나 `@` 가 든 비밀번호
#: (`&9eC/bCW@MqCfEw`)에서 매칭이 끊겨 평문이 그대로 새어나간다 — 실제로 겪었다.
#: 뒤에 host 를 요구해 두면 역추적으로 **마지막** `@` 를 경계로 잡는다.
_URL_CRED = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<user>[^\s:/@]+)"
    r"(?::(?P<pw>[^\s]+))?"
    r"@(?P<host>[^\s/@:]+)"
)

# --- ② KEY=value ------------------------------------------------------------
#
# 키 이름으로 판단한다. 맨 `KEY` 는 넣지 않는다 — `PRIMARY_KEY=id` 가 걸린다.
# `AUTH` 도 넣지 않는다 — `AUTHOR=...` 가 걸린다.
_SECRET_WORDS = (
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIALS?|AUTHORIZATION"
    r"|API[_\-]?KEY|ACCESS[_\-]?KEY|PRIVATE[_\-]?KEY|SESSION[_\-]?KEY)"
)
_ENV_ASSIGN = re.compile(
    rf"(?P<key>[A-Za-z0-9_\-.]*{_SECRET_WORDS}[A-Za-z0-9_\-.]*)"
    # JSON 의 `"api_key": "..."` 처럼 키 뒤에 따옴표가 닫히는 형태도 받는다.
    r"(?P<pre>[\"']?\s*)(?P<op>[=:])(?P<post>\s*)"
    r"(?P<q>[\"']?)(?P<val>[^\s\"'#,;)\]}]+)(?P=q)",
    re.IGNORECASE,
)

#: 값이 비밀이 아님이 명백한 경우. 여기 걸리면 원문을 그대로 둔다.
_BENIGN_LITERALS = frozenset(
    {
        "true", "false", "none", "null", "nil", "undefined", "yes", "no",
        "...", "xxx", "changeme",
        # `Authorization: Bearer xxx` 는 _BEARER 가 이미 처리했다. 여기서 "Bearer" 를
        # 값으로 오인해 다시 가리면 `Authorization: [MASKED:env] [MASKED:bearer]` 가 된다.
        "bearer", "basic", "token",
    }
)
#: 코드·템플릿 참조 — 값이 아니라 "값을 가리키는 식"이다.
#: `[` 는 앞 단계가 남긴 `[MASKED:...]` 를 다시 먹지 않게 하려는 것이기도 하다(멱등성).
_BENIGN_PREFIXES = (
    "os.", "process.", "env.", "config.", "settings.", "$", "{", "[", "<", "%", "@"
)


def _looks_benign(value: str) -> bool:
    """`KEY=value` 의 값이 비밀이 **아님이 명백한가**.

    >>> _looks_benign("16000"), _looks_benign("os.environ.get")
    (True, True)
    >>> _looks_benign("hunter2")
    False
    """
    v = value.strip()
    if len(v) < 4:  # 너무 짧아 비밀일 수 없다
        return True
    if v.isdigit():  # MAX_TOKENS=16000
        return True
    if v.lower() in _BENIGN_LITERALS:
        return True
    if v.startswith(_BENIGN_PREFIXES):  # api_key=os.environ["X"]
        return True
    if v.endswith("..."):  # .env.example 의 자리표시자
        return True
    return False


def _mask_bearer(m: re.Match[str]) -> str:
    return m.group("pre") + MASK.format(kind="bearer")


def _mask_url(m: re.Match[str]) -> str:
    # 앞 단계가 이미 가린 자리(`https://[MASKED:github]@host`)를 다시 먹으면 표기가 깨진다.
    # `[MASKED:github]` 의 콜론을 user:pw 구분자로 오인해 `[MASKED:[MASKED:url]` 이 된다.
    if "[MASKED:" in m.group(0):
        return m.group(0)

    scheme, user, pw, host = (
        m.group("scheme"), m.group("user"), m.group("pw"), m.group("host")
    )
    if pw:
        return f"{scheme}{user}:{MASK.format(kind='url')}@{host}"
    # 비밀번호 없이 사용자 자리에 토큰만 박는 형태 — 길면 토큰으로 본다.
    if len(user) >= 24:
        return f"{scheme}{MASK.format(kind='url')}@{host}"
    return m.group(0)


def _mask_env(m: re.Match[str]) -> str:
    if _looks_benign(m.group("val")):
        return m.group(0)
    q = m.group("q")
    return (
        f"{m.group('key')}{m.group('pre')}{m.group('op')}{m.group('post')}"
        f"{q}{MASK.format(kind='env')}{q}"
    )


def mask(text: str | None) -> str | None:
    r"""비밀로 보이는 부분을 `[MASKED:종류]` 로 바꾼다. None/빈 문자열은 통과.

    >>> mask("export ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf0123456789xyz")
    'export ANTHROPIC_API_KEY=[MASKED:anthropic]'
    >>> mask("DB_PASSWORD=hunter2ABC")
    'DB_PASSWORD=[MASKED:env]'
    >>> mask("fatal: unable to access https://ghp_0123456789abcdefghijklmnopqrstuvwxyz@github.com/x.git")
    'fatal: unable to access https://[MASKED:github]@github.com/x.git'
    >>> mask("psql postgresql://postgres.abcd:s3cr3tpw@aws.pooler.supabase.com:5432/postgres")
    'psql postgresql://postgres.abcd:[MASKED:url]@aws.pooler.supabase.com:5432/postgres'

    정상 코드·커밋 메시지는 건드리지 않는다:

    >>> mask("max_tokens=16000 으로 올림 (커밋 a1b2c3d4e5f67890abcdef1234567890abcdef12)")
    'max_tokens=16000 으로 올림 (커밋 a1b2c3d4e5f67890abcdef1234567890abcdef12)'
    >>> mask("api_key=os.environ['ANTHROPIC_API_KEY']")
    "api_key=os.environ['ANTHROPIC_API_KEY']"

    이미 마스킹된 텍스트를 다시 넣어도 결과가 같다 (멱등성):

    >>> once = mask("token: abcdefghijklmnop")
    >>> once == mask(once)
    True
    """
    if not text:
        return text

    for kind, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(MASK.format(kind=kind), text)

    text = _BEARER.sub(_mask_bearer, text)
    text = _URL_CRED.sub(_mask_url, text)
    text = _ENV_ASSIGN.sub(_mask_env, text)
    return text
