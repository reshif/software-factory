"""One helper that shapes every GitHub REST/App request header (final draft §13.1 #9).

Keeping this in one place means every call site — the token mint, the REST
client, the setup script — sends the same, auditable header shape.
"""
GITHUB_API_VERSION = "2022-11-28"


def github_headers(token: str) -> dict:
    """Standard headers for a bearer-authenticated GitHub call (REST or App JWT)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def next_link_url(link_header: str | None) -> str | None:
    """Extracts the `rel="next"` URL from a GitHub `Link` response header, or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = [p.strip() for p in part.split(";")]
        if len(section) < 2:
            continue
        url_part = section[0]
        if url_part.startswith("<") and url_part.endswith(">") and 'rel="next"' in section[1:]:
            return url_part[1:-1]
    return None
