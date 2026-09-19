"""Known node CSP revisions and narrowly scoped template upgrades."""

from .fokus_proxy import render_proxy as render_fokus_proxy
from .loop_proxy import render_proxy as render_loop_proxy
from .northline_proxy import render_proxy


NORTHLINE_PROXY = render_proxy()
LEGACY_NORTHLINE_PROXY = render_proxy(legacy=True)

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
MORROW_AI_NODE_CSP = ASTER_NODE_CSP.replace(
    "https://mastodon.social https://rutube.ru;",
    "https://mastodon.social https://rutube.ru https://prexzyapis.com;",
)

# Video traffic uses media-src; API calls use a fixed same-origin nginx route.
NODE_CSP = ASTER_NODE_CSP

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


def _upgrade_known_policy(text: str) -> str:
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    for policy in (LEGACY_NODE_CSP, ASTER_NODE_CSP, MORROW_AI_NODE_CSP):
        text = text.replace(f'add_header Content-Security-Policy "{policy}" always;', new)
    return text


def upgrade_aster_policy(text: str) -> str:
    """Upgrade known policies and preserve already installed Morrow routes."""
    upgraded = _upgrade_known_policy(text)
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if ASTER_SEARCH_PROXY in upgraded or new not in upgraded:
        return upgraded
    return upgraded.replace(new, f"{new}\n\n{ASTER_SEARCH_PROXY}")


def upgrade_morrow_policy(text: str) -> str:
    """Add only the fixed, anonymous Yappy read routes to known configurations."""
    upgraded = _upgrade_known_policy(text)
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if MORROW_YAPPY_PROXY in upgraded or new not in upgraded:
        return upgraded
    return upgraded.replace(new, f"{new}\n\n{MORROW_YAPPY_PROXY}")


def upgrade_answers_policy(text: str) -> str:
    """Add fixed anonymous Mail Answers read routes to known managed configs."""
    upgraded = _upgrade_known_policy(text)
    new = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if ANSWERS_MAIL_PROXY in upgraded or new not in upgraded:
        return upgraded
    return upgraded.replace(new, f"{new}\n\n{ANSWERS_MAIL_PROXY}")


ANSWERS_MAIL_PROXY = r"""    location = /_answers/mail/feed {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^limit=20(?:&pos=[0-9]{1,12})?(?:&space=[a-z0-9_-]{1,70})?$") { return 400; }
        proxy_pass https://otvet.mail.ru/api/topic/feed;
        proxy_ssl_server_name on;
        proxy_ssl_name otvet.mail.ru;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host otvet.mail.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Spros/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location = /_answers/mail/spaces {
        if ($request_method != GET) { return 405; }
        if ($args != "") { return 400; }
        proxy_pass https://otvet.mail.ru/api/topic/spaces/top;
        proxy_ssl_server_name on;
        proxy_ssl_name otvet.mail.ru;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host otvet.mail.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Spros/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location = /_answers/mail/search {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^text=[A-Za-z0-9._~%+-]{1,600}$") { return 400; }
        rewrite ^ /api/topic/search break;
        proxy_pass https://otvet.mail.ru;
        proxy_ssl_server_name on;
        proxy_ssl_name otvet.mail.ru;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host otvet.mail.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Spros/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location ~ "^/_answers/mail/question/(?<answers_id>[0-9]{1,12})$" {
        if ($request_method != GET) { return 405; }
        if ($args != "") { return 400; }
        rewrite ^ /api/topic/question/$answers_id break;
        proxy_pass https://otvet.mail.ru;
        proxy_ssl_server_name on;
        proxy_ssl_name otvet.mail.ru;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host otvet.mail.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Spros/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location ~ "^/_answers/mail/answers/(?<answers_id>[0-9]{1,12})$" {
        if ($request_method != GET) { return 405; }
        if ($args != "limit=50") { return 400; }
        rewrite ^ /api/topic/answers/$answers_id break;
        proxy_pass https://otvet.mail.ru;
        proxy_ssl_server_name on;
        proxy_ssl_name otvet.mail.ru;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host otvet.mail.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Spros/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location /_answers/ { return 404; }"""


MORROW_YAPPY_PROXY = r"""    location = /_morrow/yappy/feed {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^page=[1-9][0-9]{0,3}$") { return 400; }
        set $args "page=$arg_page&fingerprint=";
        rewrite ^ /api/feed break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location = /_morrow/yappy/search {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^query=[A-Za-z0-9_.!~*%'()-]{1,1080}&page=[1-9][0-9]{0,3}$") { return 400; }
        rewrite ^ /api/search/video break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location ~ "^/_morrow/yappy/video/(?<morrow_id>[a-f0-9]{32})$" {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^$") { return 400; }
        rewrite ^ /api/video/$morrow_id break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location ~ "^/_morrow/yappy/comments/(?<morrow_id>[a-f0-9]{32})$" {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^page=[1-9][0-9]{0,3}$") { return 400; }
        rewrite ^ /api/video/comments/$morrow_id break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location ~ "^/_morrow/yappy/author/(?<morrow_id>[a-f0-9]{32})$" {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^$") { return 400; }
        rewrite ^ /api/profile/uid/$morrow_id break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location ~ "^/_morrow/yappy/author-videos/(?<morrow_id>[a-f0-9]{32})$" {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^(?:created=[0-9A-Fa-fT:.Z%+-]{1,100})?$") { return 400; }
        rewrite ^ /api/video/list/$morrow_id break;
        proxy_pass https://yappy.media;
        proxy_ssl_server_name on;
        proxy_ssl_name yappy.media;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host yappy.media;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; MorrowVideo/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }

    location /_morrow/ { return 404; }"""


def upgrade_svod_policy(text: str) -> str:
    """Install fixed read-only encyclopedia routes in known managed configs."""
    upgraded = _upgrade_known_policy(text)
    marker = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if SVOD_WIKIPEDIA_PROXY in upgraded or marker not in upgraded:
        return upgraded
    return upgraded.replace(marker, f"{marker}\n\n{SVOD_WIKIPEDIA_PROXY}")

SVOD_WIKIPEDIA_PROXY = r"""    location = /_svod/wikipedia/search {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^q=(?<svod_q>[A-Za-z0-9._~%*+-]{1,1800})&offset=(?<svod_offset>[0-9]{1,6})$") { return 400; }
        set $args "format=json&action=query&list=search&srsearch=$svod_q&sroffset=$svod_offset&srlimit=20&srnamespace=0";
        rewrite ^ /w/api.php break;
        proxy_pass https://ru.wikipedia.org;
        proxy_ssl_server_name on;
        proxy_ssl_name ru.wikipedia.org;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host ru.wikipedia.org;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Svod/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location = /_svod/wikipedia/article {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^title=(?<svod_title>[A-Za-z0-9._~%*+-]{1,1800})$") { return 400; }
        set $args "format=json&action=parse&page=$svod_title&prop=text%7Ccategories%7Crevid&redirects=1&disableeditsection=1";
        rewrite ^ /w/api.php break;
        proxy_pass https://ru.wikipedia.org;
        proxy_ssl_server_name on;
        proxy_ssl_name ru.wikipedia.org;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host ru.wikipedia.org;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Svod/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location = /_svod/wikipedia/category {
        if ($request_method != GET) { return 405; }
        if ($args !~ "^title=(?<svod_category>[A-Za-z0-9._~%*+-]{1,1800})&continue=(?<svod_continue>[A-Za-z0-9._~%*+-]{0,3000})$") { return 400; }
        set $svod_category_args "format=json&action=query&list=categorymembers&cmtitle=$svod_category&cmlimit=30&cmtype=page%7Csubcat";
        if ($svod_continue != "") {
            set $svod_category_args "$svod_category_args&cmcontinue=$svod_continue";
        }
        set $args $svod_category_args;
        rewrite ^ /w/api.php break;
        proxy_pass https://ru.wikipedia.org;
        proxy_ssl_server_name on;
        proxy_ssl_name ru.wikipedia.org;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host ru.wikipedia.org;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Svod/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location = /_svod/wikipedia/random {
        if ($request_method != GET) { return 405; }
        if ($args != "") { return 400; }
        set $args "format=json&action=query&list=random&rnnamespace=0&rnlimit=1";
        rewrite ^ /w/api.php break;
        proxy_pass https://ru.wikipedia.org;
        proxy_ssl_server_name on;
        proxy_ssl_name ru.wikipedia.org;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host ru.wikipedia.org;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Svod/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }

    location /_svod/ { return 404; }"""


def upgrade_northline_policy(text: str) -> str:
    """Add only fixed anonymous Mastodon reads to known managed configurations."""
    upgraded = _upgrade_known_policy(text)
    upgraded = upgraded.replace(LEGACY_NORTHLINE_PROXY, NORTHLINE_PROXY)
    marker = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if NORTHLINE_PROXY in upgraded or marker not in upgraded:
        return upgraded
    return upgraded.replace(marker, f"{marker}\n\n{NORTHLINE_PROXY}")


LOOP_GIFS_PROXY = render_loop_proxy()


def upgrade_loop_policy(text: str) -> str:
    """Add anonymous Loop routes to known configurations only."""
    upgraded = _upgrade_known_policy(text)
    marker = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if LOOP_GIFS_PROXY in upgraded or marker not in upgraded:
        return upgraded
    return upgraded.replace(marker, f"{marker}\n\n{LOOP_GIFS_PROXY}")


FOKUS_RIA_PROXY = render_fokus_proxy()

def upgrade_fokus_policy(text: str) -> str:
    """Add read-only Fokus routes to known managed configurations."""
    upgraded = _upgrade_known_policy(text)
    marker = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
    if FOKUS_RIA_PROXY in upgraded or marker not in upgraded:
        return upgraded
    return upgraded.replace(marker, f"{marker}\n\n{FOKUS_RIA_PROXY}")
