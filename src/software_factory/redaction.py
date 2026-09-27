"""Conservative masking of obvious secrets in text that may leave the machine or be logged.

Stable API: ``redact(text) -> (masked_text, mask_count)``. Masks name the kind of secret
but never contain any part of it. Masking is deliberately conservative (it can mask a
harmless value that looks like an assignment to a key/token/password) and is not a
guarantee that no secret remains: keep sensitive material out of shared records.

Recognized: PEM private key blocks, passwords in URL userinfo, Slack webhook URLs,
Azure connection-string keys and SAS signatures, ``Authorization`` header values and
bearer tokens, JWTs, AWS access key IDs, ``sk-``, Stripe (``sk_``/``rk_``/``whsec_``),
Google (``AIza``), GitHub, GitLab, Hugging Face, SendGrid, npm and Slack tokens, docker
``"auth"`` and ``.npmrc`` ``_auth`` values, ``.netrc`` passwords, ``curl -u user:pass``
passwords, password/token command-line flag values, and values assigned to names
containing a key, secret, token, password, passphrase, credential or ``pass`` word.
Quoted values are masked whole; unquoted short numbers, booleans/null, builtin type
names and obvious code (``name(``, ``name[``) are left alone.

Known limits: this is a best-effort denylist. A bare high-entropy string without a
recognizable prefix or key name (for example a bare 40-character AWS secret access key)
is not masked, nor is a value on a different line from its key, an unquoted value shaped
like ``name(...``/``name[...``, a secret in an unusual format, or a secret passed as a
separate argv item except through :func:`redact_argv`. Name and flag scans are bounded
(64 characters around a key word, 1 KiB from a command name to its password flag) so
matching stays linear on adversarial input; a longer name or command line is missed.

Home-directory prefixes (the current user's home and generic ``/home/<name>``,
``/Users/<name>`` and ``C:\\Users\\<name>`` forms) are replaced with ``~`` so local
usernames do not leave the machine; each replacement counts as one mask.
"""

from __future__ import annotations

import os
import re

__all__ = ["MASK_PREFIX", "bound_text", "redact", "redact_argv"]

MASK_PREFIX = "[REDACTED:"
_NOT_MASKED = r"(?!\[REDACTED:)"
# Characters that end an unquoted assigned value.
_VALUE = r"[^\s\"'`,;<>)\]}]+"
# Groups 2-4 after a one-group prefix: optional string prefix and quote (kept), then the
# value. A quoted value runs to its closing quote or the end of the line, so every word
# of a multi-word value is masked.
_QUOTED_VALUE = (
    r"((?:[rbuf]{1,2}(?=[\"'`]))?([\"'`])?)"
    + _NOT_MASKED
    + r"((?(3)(?:\\.|(?!\3)[^\\\n])*|(?![rbuf]{1,2}[\"'`])"
    + _VALUE
    + r"))"
)
# ``pass`` counts only as a whole name part (``DB_PASS``, ``PASS_FILE``), not inside
# ``bypass``/``passed``/``compass``/``test_passes``, and not as ``pass_rate``-style metrics.
_PASS_WORD = r"(?<![A-Za-z])pass(?![A-Za-z0-9])(?![_.-](?:rate|ratio|count|percent|pct|fraction)\b)"
_KEY_WORDS = (
    r"(?:api[_-]?key|secret|token|passwd|password|passphrase|pwd|private[_-]?key|access[_-]?key|credential|"
    + _PASS_WORD
    + r")"
)
# A Python-style annotation before `` =`` (``password: str = ...``) belongs to the key.
_ANNOTATION = (
    r"(?:[ \t]*:[ \t]*[A-Za-z_][\w.]*(?:\[[^\]\n=]*\])?"
    r"(?:[ \t]*\|[ \t]*[A-Za-z_][\w.]*(?:\[[^\]\n=]*\])?)*[ \t]+=(?!=))"
)
# Assignment separators; ``==``, ``!=``, ``<=``, ``>=`` and ``::`` are not assignments.
_SEPARATOR = r"(?:" + _ANNOTATION + r"|[ \t]*(?:=>|:=|=(?!=)|:(?![:=])))[ \t]*"
# Bounded on both sides so a long run of repeated key words cannot backtrack quadratically.
_FLAG = r"--?[A-Za-z0-9_-]{0,64}(?:passwd|password|passphrase|token|secret|api[_-]?key)[A-Za-z0-9_-]{0,64}+"
_MYSQL = r"(?:mysql|mariadb|mysqldump|mysqladmin|mysqlimport|mysqlsh|mysqlcheck)"
# Rest of a command line between a program name and its password flag, bounded to 1 KiB.
_COMMAND_REST = r"\b[^\n]{0,1024}?"
# Unquoted assigned values that are ordinary code or configuration, not secrets.
_PLAIN_VALUE = re.compile(
    r"(?i)(?:[+-]?(?:\d[\d_]{0,6}(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?"
    r"|true|false|none|null|nil|undefined|str|int|float|bool|bytes|any|object"
    r"|[\[({]+|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*[(\[].*)"
)

# A home prefix ends at a path separator or at a character that cannot continue a name.
_HOME_END = r"(?=[/\\\s\"'`:;,<>)\]}]|$)"
_GENERIC_HOMES = (
    re.compile(r"(?<![\w.~/\\-])/(?:home|Users)/[^/\s\"'`:;,<>)\]}]+" + _HOME_END),
    re.compile(r"(?<![\w.~/\\-])[A-Za-z]:\\Users\\[^\\/\s\"'`:;,<>)\]}]+" + _HOME_END),
)

# Ordered: whole blocks first, then URLs and headers, then known token shapes, then flags
# and generic assignments. ``part`` is None (mask the whole match), "value" (keep group 1,
# mask group 2), "flag" or "quoted" (keep groups 1-2, mask a non-empty group 4; "quoted"
# also leaves plain unquoted values such as numbers and code).
_PATTERNS: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN[ A-Z0-9]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(?:-----END[ A-Z0-9]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
        None,
    ),
    (
        "url_password",
        re.compile(
            r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://[^\s:/@\"'`<>]*:)" + _NOT_MASKED + r"([^\s/\"'`<>]+)(?=@)"
        ),
        "value",
    ),
    (
        "slack_webhook",
        re.compile(r"(?i)(\bhttps?://hooks\.slack\.com/(?:services|workflows|triggers)/)([A-Za-z0-9/_-]+)"),
        "value",
    ),
    (
        "connection_string",
        re.compile(
            r"(?i)((?<![A-Za-z0-9])(?:AccountKey|SharedAccessKey|SharedAccessSignature)=)"
            + _NOT_MASKED
            + r"([^;\s\"'`<>]+)"
        ),
        "value",
    ),
    ("sas_signature", re.compile(r"(?i)([?&]sig=)" + _NOT_MASKED + r"([^&;\s\"'`<>#]+)"), "value"),
    (
        "authorization",
        re.compile(
            r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?"
            r"(?!(?:(?:bearer|basic|token|digest|bot)\s+)?\[REDACTED:)(?:(?:bearer|basic|token|digest|bot)\s+)?)"
            + _NOT_MASKED
            + r"("
            + _VALUE
            + r")"
        ),
        "value",
    ),
    ("bearer", re.compile(r"(?i)(\bbearer\s+)" + _NOT_MASKED + r"([A-Za-z0-9._~+/=-]{8,})"), "value"),
    # The header segment is bounded so repeated ``eyJ-`` starts cannot rescan the text.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,2048}+\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*"), None),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), None),
    ("api_token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), None),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9])(?:(?:sk|rk)_(?:live|test)_|whsec_)[A-Za-z0-9]{8,}"), None),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}"), None),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), None),
    ("npm_token", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36,}"), None),
    ("slack_token", re.compile(r"\bxox[abposre]-[A-Za-z0-9-]{10,}"), None),
    ("gitlab_token", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}"), None),
    ("huggingface_token", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), None),
    ("sendgrid_key", re.compile(r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), None),
    (
        "auth_value",
        re.compile(
            r"(?i)([\"']auth[\"'][ \t]*:[ \t]*[\"']|(?<![A-Za-z0-9])_auth[ \t]*=[ \t]*[\"']?)"
            + _NOT_MASKED
            + r"([^\s\"'`]+)"
        ),
        "value",
    ),
    (
        "password_flag",
        re.compile(
            r"(?i)(\b"
            + _MYSQL
            + _COMMAND_REST
            + r"[ \t]-p|\bsshpass[ \t]+-p[ \t]*|\b(?:docker|podman)\b[ \t]+login"
            + _COMMAND_REST
            + r"[ \t]-p[ \t]+)"
            + _NOT_MASKED
            + r"([^\s\"'`]+)"
        ),
        "value",
    ),
    (
        "password_flag",
        re.compile(
            r"(?i)(\bcurl"
            + _COMMAND_REST
            + r"[ \t](?:-[uU][ \t]*|--(?:proxy-)?user(?:=|[ \t]+))[\"']?[^\s:\"'`]*:)"
            + _NOT_MASKED
            + r"([^\s\"'`]+)"
        ),
        "value",
    ),
    ("password_flag", re.compile(r"(?i)((?<![\w-])" + _FLAG + r"(?:=|[ \t]+))" + _QUOTED_VALUE), "flag"),
    (
        # ``.netrc``: ``login NAME password VALUE`` or a line starting ``password VALUE``.
        "netrc_password",
        re.compile(
            r"((?:(?m:^)[ \t]*|\blogin[ \t]+[^\s]+[ \t]+)password[ \t]+)"
            + _NOT_MASKED
            + r"(?!(?:is|in|and|or|not|if|else|for)\b|[=:#!<>|])([^\s]+)"
        ),
        "value",
    ),
    (
        "assignment",
        re.compile(
            # The match starts at the key word (the kept name prefix changes nothing) and
            # the rest of the name is bounded, so matching stays linear on long identifiers.
            # A bare upper-case ``PASS: name`` is a test-result line, not an assignment.
            r"(?i)(?!(?<![A-Za-z0-9_.-])(?-i:PASS)[\"']?[ \t]*:(?![:=]))("
            + _KEY_WORDS
            + r"[A-Za-z0-9_.-]{0,64}+[\"']?"
            + _SEPARATOR
            + r")"
            + _QUOTED_VALUE
        ),
        "quoted",
    ),
)
_FLAG_ONLY = re.compile(r"(?i)" + _FLAG)
_MYSQL_PROGRAM = re.compile(r"(?i)" + _MYSQL + r"(?:\.exe)?")
_OTHER_PROGRAM = re.compile(r"(?i)(curl|sshpass|docker|podman)(?:\.exe)?")
_CURL_USER = re.compile(r"(-[uU]|--(?:proxy-)?user=)([^:]*:)(.+)", re.DOTALL)
_MASK_BYTES = re.compile(rb"\[REDACTED:[a-z_]+\]")


def redact(text: str) -> tuple[str, int]:
    """Return ``text`` with obvious secrets masked, and the number of masks applied."""
    if not isinstance(text, str):
        raise TypeError("redact expects text")
    count = 0
    for kind, pattern, part in _PATTERNS:
        mask = f"{MASK_PREFIX}{kind}]"

        def replace(match, mask=mask, part=part):
            nonlocal count
            if part in ("flag", "quoted"):
                quote, value = match.group(3), match.group(4)
                if not value or (part == "quoted" and not quote and _PLAIN_VALUE.fullmatch(value)):
                    return match.group(0)
                count += 1
                return match.group(1) + match.group(2) + mask
            count += 1
            return match.group(1) + mask if part == "value" else mask

        text = pattern.sub(replace, text)
    home = os.path.expanduser("~")
    homes = list(_GENERIC_HOMES)
    if os.path.isabs(home) and home.rstrip("/\\") and len(home.strip("/\\").split(os.sep)) >= 2:
        homes.insert(0, re.compile(r"(?<![\w.~/\\-])" + re.escape(home.rstrip("/\\")) + _HOME_END))
    for pattern in homes:
        text, number = pattern.subn("~", text)
        count += number
    return text, count


def redact_argv(argv) -> tuple[list[str], int]:
    """Mask each argument, plus values given as the item after a secret flag.

    ``["--password", "x"]``, MySQL-family ``["mysql", "-px"]``, ``sshpass -p x``,
    ``docker login -p x`` and the password part of ``curl -u user:pass`` are masked even
    though no single item shows the secret next to its flag. ``mysql -p NAME`` keeps
    ``NAME``, since MySQL reads a separate item after ``-p`` as the database.
    """
    masked, count, programs, previous = [], 0, set(), None
    mask = f"{MASK_PREFIX}password_flag]"
    for argument in argv:
        text, number = redact(argument)
        curl_user = _CURL_USER.fullmatch(argument) if "curl" in programs else None
        secret_flag = previous is not None and bool(_FLAG_ONLY.fullmatch(previous))
        if argument and (secret_flag or (previous == "-p" and programs & {"sshpass", "login"})):
            text, number = mask, 1
        elif "curl" in programs and previous in ("-u", "-U", "--user", "--proxy-user") and ":" in argument:
            text, number = argument.split(":", 1)[0] + ":" + mask, 1
        elif curl_user:
            text, number = curl_user.group(1) + curl_user.group(2) + mask, 1
        elif (
            programs & {"mysql", "sshpass"}
            and len(argument) > 2
            and argument.startswith("-p")
            and not text.startswith("-p" + MASK_PREFIX)
        ):
            text, number = "-p" + mask, 1
        program = os.path.basename(argument.replace("\\", "/"))
        if _MYSQL_PROGRAM.fullmatch(program):
            programs.add("mysql")
        elif match := _OTHER_PROGRAM.fullmatch(program):
            programs.add(match.group(1).lower())
        elif argument == "login" and programs & {"docker", "podman"}:
            programs.add("login")
        masked.append(text)
        count += number
        previous = argument
    return masked, count


def bound_text(text: str, limit: int, *, tail: bool = False) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes; return the text and whether it was cut.

    The cut falls on a line boundary: a partial line at the cut is dropped, never kept as
    a fragment, so a secret on that line cannot be split before masking and a
    ``[REDACTED:kind]`` mask is never cut in half. ``tail`` keeps the end instead of the start.
    """
    data = text.encode()
    if len(data) <= limit:
        return text, False
    limit = max(0, limit)
    spans = [match.span() for match in _MASK_BYTES.finditer(data)]
    if tail:
        start = len(data) - limit
        if data[start - 1 : start] != b"\n":
            newline = data.find(b"\n", start)
            start = newline + 1 if newline != -1 else len(data)
        for begin, end in spans:
            if begin < start < end:
                start = end
        return data[start:].decode("utf-8", "ignore"), True
    stop = limit
    if data[stop - 1 : stop] != b"\n" and data[stop : stop + 1] != b"\n":
        stop = data.rfind(b"\n", 0, stop) + 1
    for begin, end in spans:
        if begin < stop < end:
            stop = begin
    return data[:stop].decode("utf-8", "ignore"), True
