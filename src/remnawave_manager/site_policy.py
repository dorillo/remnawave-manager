"""Known node CSP revisions and narrowly scoped template upgrades."""

LEGACY_NODE_CSP = (
    "default-src 'self'; img-src 'self' data: blob: https:; "
    "media-src 'self' data: blob: https:; style-src 'self'; script-src 'self'; "
    "connect-src 'self' https://mastodon.social; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
ASTER_NODE_CSP = LEGACY_NODE_CSP.replace(
    "connect-src 'self' https://mastodon.social;",
    "connect-src 'self' https://mastodon.social https://rutube.ru; frame-src https://rutube.ru;",
)
NODE_CSP = ASTER_NODE_CSP.replace(
    "https://mastodon.social https://rutube.ru;",
    "https://mastodon.social https://rutube.ru https://prexzyapis.com;",
)

ASTER_SEARCH_PROXY = """    location = /_aster/rutube-search {
        limit_except GET { deny all; }
        proxy_pass https://rutube.ru/api/search/video/;
        proxy_ssl_server_name on;
        proxy_ssl_name rutube.ru;
        proxy_pass_request_headers off;
        proxy_set_header Host rutube.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; AsterVideo/1.0)";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }"""


def upgrade_aster_policy(text: str) -> str:
    """Add the exact Aster policy and search route to manager-generated configs."""
    old = f'add_header Content-Security-Policy "{LEGACY_NODE_CSP}" always;'
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    upgraded = text.replace(old, new).replace(
        f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;', new
    )
    if ASTER_SEARCH_PROXY in upgraded or new not in upgraded:
        return upgraded
    return upgraded.replace(new, f"{new}\n\n{ASTER_SEARCH_PROXY}")


def upgrade_morrow_policy(text: str) -> str:
    """Upgrade exact manager-generated policies without adding proxy routes."""
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    for policy in (LEGACY_NODE_CSP, ASTER_NODE_CSP):
        text = text.replace(f'add_header Content-Security-Policy "{policy}" always;', new)
    return text
