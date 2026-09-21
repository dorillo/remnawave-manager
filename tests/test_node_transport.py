from unittest import mock

import pytest

from remnawave_manager.adopt import inspect_inventory
from remnawave_manager.errors import ValidationError
from remnawave_manager.install import render_node_nginx
from remnawave_manager.models import Inventory, ManagedFile
from remnawave_manager.node_transport import transport_features, transport_routes
from remnawave_manager.site_config import upgrade_site_config
from remnawave_manager.site_policy import NODE_CSP, TEMPLATE_POLICIES


def route(provider, target="http://xray_xhttp"):
    return f'''location ^~ /cdn-assets/example/ {{
    # remnawave-manager: transport=xhttp provider={provider}
    proxy_pass {target};
    proxy_buffering off;
    proxy_request_buffering off;
    gzip off;
}}'''


UPSTREAM = "upstream xray_xhttp { server 127.0.0.1:2090; }\n"


@pytest.mark.parametrize("provider,feature", [
    ("none", "xhttp_direct"), ("yandex", "yandex_cdn"),
    ("beeline-get", "beeline_cdn_get"), ("beeline-post", "beeline_cdn_post"),
])
def test_explicit_provider_is_independent_of_upstream_name(provider, feature):
    features = transport_features(UPSTREAM + "server {" + route(provider) + "}")
    assert features[feature]
    assert features["xhttp_provider_explicit"]
    assert not features["xhttp_provider_unknown"]


def test_generic_upstream_does_not_guess_yandex():
    features = transport_features(UPSTREAM + 'server { location /cdn-assets/test { proxy_pass http://xray_xhttp; } }')
    assert features["xhttp_stream_separation"]
    assert features["xhttp_provider_unknown"]
    assert not features["yandex_cdn"]


@pytest.mark.parametrize("text", [
    'server { listen unix:/dev/shm/nginx.sock ssl proxy_protocol; }',
    '# xhttp\nserver { location / { proxy_pass https://cdn.yandex.net; } }',
    'server { return 200 "# remnawave-manager: transport=xhttp provider=yandex"; }',
])
def test_site_content_and_fallback_socket_are_not_xhttp(text):
    assert not any(transport_features(text).values())


@pytest.mark.parametrize("text", [
    route("yandex", "https://avatars.mds.yandex.net"),
    route("invalid"),
    "# remnawave-manager: transport=xhttp provider=yandex\n" + UPSTREAM,
    UPSTREAM + route("yandex").replace("proxy_buffering off;", "# remnawave-manager: transport=xhttp provider=none"),
    UPSTREAM + route("yandex").replace("proxy_pass http://xray_xhttp;", ""),
])
def test_invalid_or_orphaned_metadata_is_rejected(text):
    with pytest.raises(ValidationError):
        transport_features(text)


def test_mixed_providers_and_quoted_proxy_target():
    text = UPSTREAM + route("yandex", '"http://xray_xhttp"') + route("none", "http://unix:/dev/shm/direct.socket")
    features = transport_features(text)
    assert features["yandex_cdn"] and features["xhttp_direct"]


@pytest.mark.parametrize("template", TEMPLATE_POLICIES)
@pytest.mark.parametrize("provider", ["none", "yandex", "beeline-post"])
def test_site_replacement_preserves_marked_transport_byte_for_byte(template, provider):
    transport = route(provider)
    original = UPSTREAM + f'''server {{
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    root /var/www/html;
    add_header Content-Security-Policy "{NODE_CSP}" always;
    {transport}
    location / {{ try_files $uri /index.html; }}
}}'''
    updated, count = upgrade_site_config(original, template, {"/var/www/html"})
    assert count == 1
    assert transport in updated
    assert transport_features(updated) == transport_features(original)
    again, _ = upgrade_site_config(updated, template, {"/var/www/html"})
    assert updated == again


def test_inventory_refresh_does_not_accept_drift_or_modify_saved_object(tmp_path):
    config = tmp_path / "nginx.conf"
    config.write_text(UPSTREAM + route("yandex"))
    inv = Inventory(1, "node", str(tmp_path), str(tmp_path / "compose.yml"), None, "nginx",
                    nginx_files=[str(config)], managed_files=[ManagedFile(str(config), "0" * 64, "nginx")],
                    features={"yandex_cdn": False, "certbot_renewal": True})
    before = inv.to_dict()
    with mock.patch("remnawave_manager.adopt._warp_interfaces", return_value=["warp"]):
        current = inspect_inventory(mock.Mock(), inv)
    assert current.features["yandex_cdn"] and current.features["warp"]
    assert current.features["certbot_renewal"]
    assert current.managed_files[0].sha256 == "0" * 64
    assert inv.to_dict() == before


def test_fresh_node_is_not_inventoried_as_xhttp():
    certificate = mock.Mock(fullchain="/etc/ssl/fullchain.pem", private_key="/etc/ssl/key.pem")
    text = render_node_nginx(domain="node.example.com", certificate=certificate)
    assert "gzip on;" in text
    assert not transport_routes(text)
