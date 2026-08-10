from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .adopt import adopt
from .api import RemnawaveApi, validate_admin_password
from .certificates import (
    CertificateMaterial,
    CertificateSpec,
    normalize_domain,
    obtain_certificate,
)
from .compose import compose_command
from .errors import TransactionError, ValidationError
from .firewall import FirewallTransaction, apply_firewall_transactional, plan_firewall
from .health import (
    check_panel_http,
    check_subscription_api_scopes,
    check_subscription_http,
    wait_container,
    wait_for_paths,
    wait_node_runtime,
)
from .journal import TransactionJournal
from .models import Component, Inventory, ManagedFile, Role
from .nginx import GZIP_BLOCK
from .registry import pull_verified
from .runner import (
    Runner,
    atomic_copy,
    atomic_write_json,
    atomic_write_text,
    command_exists,
    read_stable_regular_file,
    require_root,
    require_ubuntu_2404,
    sha256_file,
)
from .state import StateStore

POSTGRES_IMAGE = (
    "postgres:18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a"
)
VALKEY_IMAGE = (
    "valkey/valkey:9.0.3-alpine@sha256:"
    "e1095c6c76ee982cb2d1e07edbb7fb2a53606630a1d810d5a47c9f646b708bf5"
)
NGINX_IMAGE = (
    "nginx:1.28.0-alpine@sha256:"
    "30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284"
)
_INSTALL_MARKER_NAME = ".rwm-install-state.json"
_INSTALL_MARKER_SCHEMA = 1
_BOOTSTRAP_CREDENTIALS_NAME = ".bootstrap-credentials.json"
_MAX_BOOTSTRAP_CREDENTIALS_SIZE = 64 * 1024
_MAX_INSTALL_MARKER_SIZE = 64 * 1024
_PANEL_PROJECT_NAME = "remnawave"
_NODE_PROJECT_NAME = "remnanode"


@dataclass(frozen=True, slots=True)
class PanelInstallOptions:
    panel_domain: str
    subscription_domain: str
    certificate: CertificateSpec
    install_dir: Path = Path("/opt/remnawave")
    admin_username: str | None = None
    admin_password: str | None = None
    api_token_days: int = 365
    configure_ufw: bool = True
    ssh_ports: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class NodeInstallOptions:
    domain: str
    panel_ip: str
    secret_key: str
    certificate: CertificateSpec
    site_source: Path
    install_dir: Path = Path("/opt/remnanode")
    configure_ufw: bool = True
    ssh_ports: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class PanelInstallResult:
    inventory: Inventory
    access_url: str
    admin_username: str
    admin_password: str


@dataclass(frozen=True, slots=True)
class NodeInstallResult:
    inventory: Inventory
    domain: str


@dataclass(frozen=True, slots=True)
class PanelEnvironment:
    panel_domain: str
    subscription_domain: str
    app_secret: str
    postgres_password: str
    metrics_password: str
    webhook_secret: str


ApiFactory = Callable[[str], RemnawaveApi]


def render_panel_env(environment: PanelEnvironment) -> str:
    panel_domain = normalize_domain(environment.panel_domain)
    subscription_domain = normalize_domain(environment.subscription_domain)
    _hex_secret(environment.app_secret, 128, "APP_SECRET")
    _hex_secret(environment.postgres_password, 64, "пароль PostgreSQL")
    _hex_secret(environment.metrics_password, 128, "пароль метрик")
    _hex_secret(environment.webhook_secret, 64, "секрет webhook")
    database_url = (
        "postgresql://remnawave:"
        + environment.postgres_password
        + "@remnawave-db:5432/remnawave"
    )
    return (
        "# Remnawave Panel 3.2.3. Файл содержит секреты.\n"
        "APP_PORT=3000\n"
        "METRICS_PORT=3001\n"
        "API_INSTANCES=1\n"
        f"DATABASE_URL={_dotenv_value(database_url)}\n"
        "REDIS_SOCKET=/var/run/valkey/valkey.sock\n"
        f"APP_SECRET={environment.app_secret}\n"
        f"PANEL_DOMAIN={panel_domain}\n"
        f"FRONT_END_DOMAIN={panel_domain}\n"
        f"SUB_PUBLIC_DOMAIN={subscription_domain}\n"
        "METRICS_USER=metrics\n"
        f"METRICS_PASS={environment.metrics_password}\n"
        "IS_TELEGRAM_NOTIFICATIONS_ENABLED=false\n"
        "TELEGRAM_BOT_TOKEN=change_me\n"
        "TELEGRAM_NOTIFY_USERS=change_me\n"
        "TELEGRAM_NOTIFY_NODES=change_me\n"
        "TELEGRAM_NOTIFY_CRM=change_me\n"
        "TELEGRAM_NOTIFY_SERVICE=change_me\n"
        "TELEGRAM_NOTIFY_TBLOCKER=change_me\n"
        "WEBHOOK_ENABLED=false\n"
        "WEBHOOK_URL=https://example.invalid/webhook\n"
        f"WEBHOOK_SECRET_HEADER={environment.webhook_secret}\n"
        "BANDWIDTH_USAGE_NOTIFICATIONS_ENABLED=false\n"
        "BANDWIDTH_USAGE_NOTIFICATIONS_THRESHOLD=[60,80]\n"
        "NOT_CONNECTED_USERS_NOTIFICATIONS_ENABLED=false\n"
        "NOT_CONNECTED_USERS_NOTIFICATIONS_AFTER_HOURS=[6,24,48]\n"
        "EXPIRATION_NOTIFICATIONS_ENABLED=false\n"
        "EXPIRATION_NOTIFICATIONS=[-72,-48,-24,24]\n"
        "EXPORT_TO_STREAM_ENABLED=false\n"
        "EXPORT_TO_STREAM_MAXLEN=3000\n"
        "POSTGRES_USER=remnawave\n"
        f"POSTGRES_PASSWORD={environment.postgres_password}\n"
        "POSTGRES_DB=remnawave\n"
    )


def render_subscription_env(api_token: str) -> str:
    token = _secret_value(api_token, "API-токен Subscription Page")
    return (
        "# Remnawave Subscription Page 8.0.0. Файл содержит секрет.\n"
        "APP_PORT=3010\n"
        "REMNAWAVE_PANEL_URL=http://remnawave:3000\n"
        f"REMNAWAVE_API_TOKEN={_dotenv_value(token)}\n"
        "TRUST_PROXY=1\n"
        "MARZBAN_LEGACY_LINK_ENABLED=false\n"
    )


def render_node_env(secret_key: str) -> str:
    selected = _secret_value(secret_key, "SECRET_KEY ноды")
    return (
        "# Remnawave Node 3.1.1. Файл содержит секрет.\n"
        "NODE_PORT=2222\n"
        f"SECRET_KEY={_dotenv_value(selected)}\n"
    )


def render_panel_compose(
    *,
    panel_image: str,
    subscription_image: str,
    certificate: CertificateMaterial,
) -> str:
    for image in (panel_image, subscription_image, POSTGRES_IMAGE, VALKEY_IMAGE, NGINX_IMAGE):
        _pinned_image(image)
    certificate_mounts = _compose_mount_lines(certificate.compose_mounts())
    return f"""services:
  remnawave-db:
    image: {POSTGRES_IMAGE}
    container_name: remnawave-db
    hostname: remnawave-db
    restart: unless-stopped
    shm_size: 512mb
    env_file:
      - .env
    environment:
      TZ: UTC
    ports:
      - "127.0.0.1:6767:5432"
    volumes:
      - type: bind
        source: ./data/postgres
        target: /var/lib/postgresql
    networks:
      - remnawave-network
    logging: &logging
      driver: json-file
      options:
        max-size: "100m"
        max-file: "5"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
      interval: 3s
      timeout: 10s
      retries: 10

  remnawave-redis:
    image: {VALKEY_IMAGE}
    container_name: remnawave-redis
    hostname: remnawave-redis
    restart: unless-stopped
    command: >-
      valkey-server --save "" --appendonly no --maxmemory-policy noeviction
      --loglevel warning --unixsocket /var/run/valkey/valkey.sock
      --unixsocketperm 777 --port 0
    volumes:
      - valkey-socket:/var/run/valkey
    networks:
      - remnawave-network
    logging: *logging
    healthcheck:
      test: ["CMD", "valkey-cli", "-s", "/var/run/valkey/valkey.sock", "ping"]
      interval: 3s
      timeout: 3s
      retries: 10

  remnawave:
    image: {panel_image}
    container_name: remnawave
    hostname: remnawave
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "127.0.0.1:3000:3000"
      - "127.0.0.1:3001:3001"
    volumes:
      - valkey-socket:/var/run/valkey
    networks:
      - remnawave-network
    logging: *logging
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:3001/health >/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    depends_on:
      remnawave-db:
        condition: service_healthy
      remnawave-redis:
        condition: service_healthy

  remnawave-subscription-page:
    image: {subscription_image}
    container_name: remnawave-subscription-page
    hostname: remnawave-subscription-page
    restart: unless-stopped
    env_file:
      - .env.subscription
    ports:
      - "127.0.0.1:3010:3010"
    networks:
      - remnawave-network
    logging: *logging
    depends_on:
      remnawave:
        condition: service_healthy

  remnawave-nginx:
    image: {NGINX_IMAGE}
    container_name: remnawave-nginx
    hostname: remnawave-nginx
    restart: unless-stopped
    network_mode: host
    read_only: true
    security_opt:
      - no-new-privileges:true
    volumes:
      - type: bind
        source: ./nginx.conf
        target: /etc/nginx/conf.d/default.conf
        read_only: true
{certificate_mounts}
    tmpfs:
      - /var/cache/nginx
      - /var/run
    logging: *logging
    depends_on:
      remnawave:
        condition: service_healthy
      remnawave-subscription-page:
        condition: service_healthy

networks:
  remnawave-network:
    name: remnawave-network
    driver: bridge

volumes:
  valkey-socket:
    name: valkey-socket
    driver: local
"""


def render_node_compose(
    *,
    node_image: str,
    certificate: CertificateMaterial,
) -> str:
    for image in (node_image, NGINX_IMAGE):
        _pinned_image(image)
    certificate_mounts = _compose_mount_lines(certificate.compose_mounts())
    xray_certificate_mounts = _compose_mount_lines(
        certificate.compose_mounts(
            container_root="/var/lib/remnawave/configs/xray/ssl"
        )
    )
    return f"""services:
  remnawave-nginx:
    image: {NGINX_IMAGE}
    container_name: remnawave-nginx
    hostname: remnawave-nginx
    restart: unless-stopped
    network_mode: host
    read_only: true
    security_opt:
      - no-new-privileges:true
    volumes:
      - type: bind
        source: ./nginx.conf
        target: /etc/nginx/conf.d/default.conf
        read_only: true
      - type: bind
        source: ./site
        target: /var/www/html
        read_only: true
      - /dev/shm:/dev/shm:rw
{certificate_mounts}
    tmpfs:
      - /var/cache/nginx
      - /var/run
    command: ["sh", "-c", "rm -f /dev/shm/nginx.sock && exec nginx -g 'daemon off;'"]
    logging: &logging
      driver: json-file
      options:
        max-size: "100m"
        max-file: "5"

  remnanode:
    image: {node_image}
    container_name: remnanode
    hostname: remnanode
    restart: unless-stopped
    network_mode: host
    env_file:
      - .env
    security_opt:
      - no-new-privileges:true
    cap_add:
      - NET_ADMIN
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    volumes:
      - /dev/shm:/dev/shm:rw
{xray_certificate_mounts}
      - type: bind
        source: ./logs
        target: /var/log/xray
    logging: *logging
"""


def render_panel_nginx(
    *,
    panel_domain: str,
    subscription_domain: str,
    certificate: CertificateMaterial,
    cookie_name: str,
    cookie_value: str,
    gate_path: str,
) -> str:
    panel = normalize_domain(panel_domain)
    subscription = normalize_domain(subscription_domain)
    if panel == subscription:
        raise ValidationError("Домены Panel и Subscription Page должны отличаться.")
    if not re.fullmatch(r"rwm_[a-z0-9]{16,64}", cookie_name):
        raise ValidationError("Некорректное имя защитной cookie.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", cookie_value):
        raise ValidationError("Некорректное значение защитной cookie.")
    if not re.fullmatch(r"/_rwm/[A-Za-z0-9_-]{32,128}", gate_path):
        raise ValidationError("Некорректный секретный путь Panel.")
    cert = _nginx_path(certificate.fullchain)
    key = _nginx_path(certificate.private_key)
    return f"""server_names_hash_bucket_size 128;
map_hash_bucket_size 128;
server_tokens off;

{GZIP_BLOCK}
map $http_upgrade $connection_upgrade {{
    default upgrade;
    "" close;
}}

map $cookie_{cookie_name} $panel_authorized {{
    default 0;
    "{cookie_value}" 1;
}}

map $uri $rwm_auth_key {{
    default "";
    ~^/api/auth/(?:login|register)$ $binary_remote_addr;
}}

limit_req_zone $rwm_auth_key zone=rwm_auth:10m rate=10r/m;

upstream remnawave_panel {{
    server 127.0.0.1:3000;
    keepalive 32;
}}

upstream remnawave_subscription {{
    server 127.0.0.1:3010;
    keepalive 16;
}}

ssl_protocols TLSv1.2 TLSv1.3;
ssl_ecdh_curve X25519:prime256v1:secp384r1;
ssl_session_timeout 1d;
ssl_session_cache shared:RemnawaveTLS:10m;
ssl_session_tickets off;

server {{
    listen 80 default_server;
    server_name _;
    return 444;
}}

server {{
    listen 80;
    server_name {panel} {subscription};
    return 308 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    http2 on;
    server_name {panel};

    ssl_certificate "{cert}";
    ssl_certificate_key "{key}";
    ssl_trusted_certificate "{cert}";

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    location = {gate_path} {{
        access_log off;
        add_header Cache-Control "no-store" always;
        add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy no-referrer always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
        add_header Set-Cookie "{cookie_name}={cookie_value}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict" always;
        return 302 /auth/login;
    }}

    location / {{
        if ($panel_authorized = 0) {{ return 404; }}
        limit_req zone=rwm_auth burst=10 nodelay;
        limit_req_status 429;
        proxy_http_version 1.1;
        proxy_pass http://remnawave_panel;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port 443;
        proxy_connect_timeout 5s;
        proxy_read_timeout 240s;
        proxy_send_timeout 240s;
        proxy_hide_header X-Powered-By;
    }}
}}

server {{
    listen 443 ssl;
    http2 on;
    server_name {subscription};
    access_log off;

    ssl_certificate "{cert}";
    ssl_certificate_key "{key}";
    ssl_trusted_certificate "{cert}";

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    location / {{
        proxy_http_version 1.1;
        proxy_pass http://remnawave_subscription;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port 443;
        proxy_connect_timeout 5s;
        proxy_read_timeout 240s;
        proxy_send_timeout 240s;
        proxy_hide_header X-Powered-By;
    }}
}}

server {{
    listen 443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}}
"""


def render_node_nginx(
    *,
    domain: str,
    certificate: CertificateMaterial,
) -> str:
    selected_domain = normalize_domain(domain)
    cert = _nginx_path(certificate.fullchain)
    key = _nginx_path(certificate.private_key)
    return f"""server_names_hash_bucket_size 128;
server_tokens off;

{GZIP_BLOCK}
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ecdh_curve X25519:prime256v1:secp384r1;
ssl_session_timeout 1d;
ssl_session_cache shared:RemnawaveTLS:10m;
ssl_session_tickets off;

server {{
    listen 80 default_server;
    server_name _;
    return 444;
}}

server {{
    listen 80;
    server_name {selected_domain};
    return 308 https://$host$request_uri;
}}

server {{
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    http2 on;
    server_name {selected_domain};

    ssl_certificate "{cert}";
    ssl_certificate_key "{key}";
    ssl_trusted_certificate "{cert}";

    root /var/www/html;
    index index.html;
    real_ip_header proxy_protocol;
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" always;
    add_header Cross-Origin-Opener-Policy same-origin always;
    add_header Cross-Origin-Resource-Policy same-origin always;

    location ~ (^|/)\\. {{
        return 404;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}

    location ~* \\.(?:css|js|png|jpg|jpeg|webp|avif|ico|woff2)$ {{
        expires 7d;
        try_files $uri =404;
    }}
}}

server {{
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server;
    server_name _;
    ssl_reject_handshake on;
}}
"""


def install_panel(
    runner: Runner,
    store: StateStore,
    options: PanelInstallOptions,
    *,
    api_factory: ApiFactory = RemnawaveApi,
) -> PanelInstallResult:
    _preflight(
        runner,
        store,
        options.install_dir,
        expected=Path("/opt/remnawave"),
        role="panel",
    )
    panel_domain = normalize_domain(options.panel_domain)
    subscription_domain = normalize_domain(options.subscription_domain)
    if panel_domain == subscription_domain:
        raise ValidationError("Домены Panel и Subscription Page должны отличаться.")
    username, password = _admin_credentials(options.admin_username, options.admin_password)
    if (
        isinstance(options.api_token_days, bool)
        or not isinstance(options.api_token_days, int)
        or not 1 <= options.api_token_days <= 3650
    ):
        raise ValidationError("Срок токена Subscription Page должен быть от 1 до 3650 дней.")
    _ensure_container_names_available(
        runner,
        ("remnawave-db", "remnawave-redis", "remnawave", "remnawave-subscription-page", "remnawave-nginx"),
    )
    firewall = (
        plan_firewall(runner, "panel", ssh_ports=options.ssh_ports)
        if options.configure_ufw
        else None
    )

    registry = str(store.load_settings().get("registry", "docker-hub"))
    if registry != "docker-hub":
        raise ValidationError(
            "Для установки Panel stack выберите docker-hub: проверенный образ "
            "Subscription Page отсутствует в GHCR compatibility manifest. "
            "Выполните rwm registry select docker-hub."
        )
    panel_image = _pull_component_image(runner, "panel", registry)
    subscription_image = _pull_component_image(runner, "subscription", registry)
    _pull_base_images(runner, (POSTGRES_IMAGE, VALKEY_IMAGE, NGINX_IMAGE))

    directory = options.install_dir
    _start_install_attempt(directory, "panel")
    compose_path = directory / "docker-compose.yml"
    env_path = directory / ".env"
    subscription_env = directory / ".env.subscription"
    nginx_path = directory / "nginx.conf"
    bootstrap_credentials = directory / _BOOTSTRAP_CREDENTIALS_NAME
    started = [
        "remnawave-db",
        "remnawave-redis",
        "remnawave",
        "remnawave-subscription-page",
        "remnawave-nginx",
    ]
    firewall_transaction: FirewallTransaction | None = None
    certificate: CertificateMaterial | None = None
    secrets_payload: dict[str, str] | None = None
    result: PanelInstallResult
    try:
        _prepare_panel_directories(runner, directory)
        if firewall is not None:
            firewall_transaction = apply_firewall_transactional(
                runner,
                firewall,
                transaction_root=store.paths.state / "firewall-transactions",
            )
        certificate = obtain_certificate(
            runner,
            (panel_domain, subscription_domain),
            options.certificate,
            install_dir=directory,
        )
        environment = PanelEnvironment(
            panel_domain=panel_domain,
            subscription_domain=subscription_domain,
            app_secret=secrets.token_hex(64),
            postgres_password=secrets.token_hex(32),
            metrics_password=secrets.token_hex(64),
            webhook_secret=secrets.token_hex(32),
        )
        cookie_name = "rwm_" + secrets.token_hex(12)
        cookie_value = secrets.token_urlsafe(48)
        gate_path = "/_rwm/" + secrets.token_urlsafe(36)
        atomic_write_text(env_path, render_panel_env(environment), mode=0o600)
        atomic_write_text(
            subscription_env,
            render_subscription_env("TOKEN_WILL_BE_CREATED_AFTER_PANEL_START"),
            mode=0o600,
        )
        atomic_write_text(
            nginx_path,
            render_panel_nginx(
                panel_domain=panel_domain,
                subscription_domain=subscription_domain,
                certificate=certificate,
                cookie_name=cookie_name,
                cookie_value=cookie_value,
                gate_path=gate_path,
            ),
            mode=0o600,
        )
        atomic_write_text(
            compose_path,
            render_panel_compose(
                panel_image=panel_image,
                subscription_image=subscription_image,
                certificate=certificate,
            ),
            mode=0o600,
        )
        atomic_write_json(
            bootstrap_credentials,
            {"имя_администратора": username, "пароль_администратора": password},
            mode=0o600,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "config",
                "-q",
                env_file=env_path,
            ),
            cwd=directory,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "up",
                "-d",
                "remnawave-db",
                "remnawave-redis",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(runner, _component("database", "remnawave-db"), require_health=True)
        wait_container(runner, _component("cache", "remnawave-redis"), require_health=True)
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "up",
                "-d",
                "--no-deps",
                "remnawave",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(
            runner,
            _component("panel", "remnawave"),
            timeout=600,
            require_health=True,
        )
        api = api_factory("http://127.0.0.1:3000")
        _wait_api_ready(api)
        admin_token = api.register_or_login(username, password)
        api_token = api.create_subscription_token(
            admin_token,
            name="subscription-page",
            expires_days=options.api_token_days,
        )
        atomic_write_text(subscription_env, render_subscription_env(api_token), mode=0o600)
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "config",
                "-q",
                env_file=env_path,
            ),
            cwd=directory,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "up",
                "-d",
                "--no-deps",
                "remnawave-subscription-page",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(
            runner,
            _component("subscription", "remnawave-subscription-page"),
            require_health=True,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _PANEL_PROJECT_NAME,
                "up",
                "-d",
                "--no-deps",
                "remnawave-nginx",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(runner, _component("nginx", "remnawave-nginx"))
        runner.run(["docker", "exec", "remnawave-nginx", "nginx", "-t"])
        check_panel_http(runner, _component("panel", "remnawave"))
        check_subscription_http(
            runner,
            _component("subscription", "remnawave-subscription-page"),
        )
        check_subscription_api_scopes(
            runner,
            _component("panel", "remnawave"),
            _component("subscription", "remnawave-subscription-page"),
        )
        inventory = adopt(runner, store, directory=directory, requested_role="panel")
        extras = [
            subscription_env,
            *_certificate_secret_paths(directory, options.certificate, certificate),
        ]
        _add_managed_files(store, inventory, extras, kind="secret")
        access_url = f"https://{panel_domain}{gate_path}"
        store.initialize()
        secrets_payload = {
            "panel_access_url": access_url,
            "panel_admin_username": username,
            "panel_cookie_mode": "manager-path",
        }
        atomic_write_json(store.paths.secrets, secrets_payload, mode=0o600)
        result = PanelInstallResult(inventory, access_url, username, password)
        _finish_install_attempt(directory, "panel")
        if firewall_transaction is not None:
            firewall_transaction.commit()
        certificate.commit()
    except BaseException as error:
        compose_rollback = _stop_failed_install_containers(
            runner,
            compose_path,
            env_path,
            directory,
            _PANEL_PROJECT_NAME,
        )
        state_rollback = _rollback_failed_install_state_safely(
            store,
            directory,
            "panel",
            secrets_payload=secrets_payload,
        )
        if state_rollback is not None:
            retained = _retained_install_resources_note(firewall_transaction, certificate)
            compose_note = _cleanup_error_note(compose_rollback)
            raise TransactionError(
                "Чистая установка Panel не завершена, manager state не удалось вернуть: "
                f"{state_rollback}. {retained}.{compose_note} Исходная ошибка: {error}"
            ) from error
        resource_rollback = _rollback_install_resources(
            runner,
            firewall_transaction,
            certificate,
            started,
        )
        if resource_rollback is not None:
            compose_note = _cleanup_error_note(compose_rollback)
            raise TransactionError(
                "Чистая установка Panel не завершена, rollback внешних ресурсов выполнен "
                f"не полностью: {resource_rollback}.{compose_note} Исходная ошибка: {error}"
            ) from error
        ufw_note = _restored_install_resources_note(firewall_transaction, certificate)
        compose_note = _cleanup_error_note(compose_rollback)
        raise TransactionError(
            "Чистая установка Panel не завершена. Созданные контейнеры удалены, "
            f"конфигурация оставлена в {directory}{ufw_note}.{compose_note} "
            f"Если регистрация уже прошла, временные учётные данные находятся "
            f"в {bootstrap_credentials}: {error}"
        ) from error

    return result


def complete_panel_credentials_handoff(result: PanelInstallResult) -> None:
    """Remove the recovery copy only after the caller durably displayed credentials."""
    path = Path(result.inventory.install_dir) / _BOOTSTRAP_CREDENTIALS_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TransactionError(
            f"Временный файл учётных данных изменил тип: {path}. "
            "Он не удалён; проверьте путь вручную."
        )
    if not path.is_file():
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TransactionError(
                f"Временный файл учётных данных имеет небезопасный тип: {path}."
            )
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise TransactionError(
                f"Временный файл учётных данных должен принадлежать root и быть приватным: {path}."
            )
        if info.st_size > _MAX_BOOTSTRAP_CREDENTIALS_SIZE:
            raise TransactionError(
                f"Временный файл учётных данных имеет неожиданный размер: {path}."
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw_payload = stream.read(_MAX_BOOTSTRAP_CREDENTIALS_SIZE + 1)
        if len(raw_payload) > _MAX_BOOTSTRAP_CREDENTIALS_SIZE:
            raise TransactionError(
                f"Временный файл учётных данных имеет неожиданный размер: {path}."
            )
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise TransactionError(
                f"Временный файл учётных данных был подменён во время проверки: {path}."
            )
        payload = json.loads(raw_payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TransactionError(
            f"Не удалось безопасно проверить временный файл учётных данных {path}: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected = {
        "имя_администратора": result.admin_username,
        "пароль_администратора": result.admin_password,
    }
    if payload != expected:
        raise TransactionError(
            f"Временный файл учётных данных {path} изменился после установки и не удалён."
        )
    try:
        path.unlink()
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as error:
        raise TransactionError(
            "Panel установлена, а пароль выдан, но временный файл учётных данных "
            f"не удалось удалить: {path}. Удалите его вручную: {error}"
        ) from error


def install_node(
    runner: Runner,
    store: StateStore,
    options: NodeInstallOptions,
) -> NodeInstallResult:
    _preflight(
        runner,
        store,
        options.install_dir,
        expected=Path("/opt/remnanode"),
        role="node",
    )
    domain = normalize_domain(options.domain)
    _secret_value(options.secret_key, "SECRET_KEY ноды")
    _validate_site_source(options.site_source)
    _ensure_container_names_available(runner, ("remnanode", "remnawave-nginx"))
    firewall = (
        plan_firewall(
            runner,
            "node",
            panel_ip=options.panel_ip,
            ssh_ports=options.ssh_ports,
        )
        if options.configure_ufw
        else None
    )
    registry = str(store.load_settings().get("registry", "docker-hub"))
    node_image = _pull_component_image(runner, "node", registry)
    _pull_base_images(runner, (NGINX_IMAGE,))

    directory = options.install_dir
    _start_install_attempt(directory, "node")
    compose_path = directory / "docker-compose.yml"
    env_path = directory / ".env"
    nginx_path = directory / "nginx.conf"
    started = ("remnanode", "remnawave-nginx")
    firewall_transaction: FirewallTransaction | None = None
    certificate: CertificateMaterial | None = None
    result: NodeInstallResult
    try:
        _prepare_node_directories(directory)
        if firewall is not None:
            firewall_transaction = apply_firewall_transactional(
                runner,
                firewall,
                transaction_root=store.paths.state / "firewall-transactions",
            )
        certificate = obtain_certificate(
            runner,
            (domain,),
            options.certificate,
            install_dir=directory,
            stop_nginx_for_http01=False,
        )
        _install_static_site(options.site_source, directory / "site")
        atomic_write_text(env_path, render_node_env(options.secret_key), mode=0o600)
        atomic_write_text(
            nginx_path,
            render_node_nginx(domain=domain, certificate=certificate),
            mode=0o600,
        )
        atomic_write_text(
            compose_path,
            render_node_compose(node_image=node_image, certificate=certificate),
            mode=0o600,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _NODE_PROJECT_NAME,
                "config",
                "-q",
                env_file=env_path,
            ),
            cwd=directory,
        )
        runner.run(
            _install_compose_command(
                compose_path,
                _NODE_PROJECT_NAME,
                "up",
                "-d",
                "--no-deps",
                "remnawave-nginx",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(runner, _component("nginx", "remnawave-nginx"))
        wait_for_paths(
            [
                "/dev/shm/nginx.sock"  # noqa: S108, RUF100 - intentional shared XHTTP Unix socket
            ]
        )
        runner.run(["docker", "exec", "remnawave-nginx", "nginx", "-t"])
        runner.run(
            _install_compose_command(
                compose_path,
                _NODE_PROJECT_NAME,
                "up",
                "-d",
                "--no-deps",
                "remnanode",
                env_file=env_path,
            ),
            cwd=directory,
        )
        wait_container(runner, _component("node", "remnanode"), timeout=300)
        wait_node_runtime(
            runner,
            Inventory(
                schema_version=1,
                role="node",
                install_dir=str(directory),
                compose_file=str(compose_path),
                env_file=str(env_path),
                webserver="nginx",
                components={"node": _component("node", "remnanode")},
            ),
        )
        inventory = adopt(runner, store, directory=directory, requested_role="node")
        extras = _certificate_secret_paths(directory, options.certificate, certificate)
        _add_managed_files(store, inventory, extras, kind="secret")
        result = NodeInstallResult(inventory, domain)
        _finish_install_attempt(directory, "node")
        if firewall_transaction is not None:
            firewall_transaction.commit()
        certificate.commit()
    except BaseException as error:
        compose_rollback = _stop_failed_install_containers(
            runner,
            compose_path,
            env_path,
            directory,
            _NODE_PROJECT_NAME,
        )
        state_rollback = _rollback_failed_install_state_safely(
            store,
            directory,
            "node",
        )
        if state_rollback is not None:
            retained = _retained_install_resources_note(firewall_transaction, certificate)
            compose_note = _cleanup_error_note(compose_rollback)
            raise TransactionError(
                "Чистая установка Node не завершена, manager state не удалось вернуть: "
                f"{state_rollback}. {retained}.{compose_note} Исходная ошибка: {error}"
            ) from error
        resource_rollback = _rollback_install_resources(
            runner,
            firewall_transaction,
            certificate,
            started,
        )
        if resource_rollback is not None:
            compose_note = _cleanup_error_note(compose_rollback)
            raise TransactionError(
                "Чистая установка Node не завершена, rollback внешних ресурсов выполнен "
                f"не полностью: {resource_rollback}.{compose_note} Исходная ошибка: {error}"
            ) from error
        ufw_note = (
            _restored_install_resources_note(firewall_transaction, certificate)
        )
        compose_note = _cleanup_error_note(compose_rollback)
        raise TransactionError(
            "Чистая установка Node не завершена. Созданные контейнеры удалены, "
            f"конфигурация оставлена в {directory}{ufw_note}.{compose_note} "
            f"Исходная ошибка: {error}"
        ) from error
    return result


def _install_compose_command(
    compose_path: Path,
    project_name: str,
    *arguments: str,
    env_file: Path | None = None,
) -> list[str]:
    if project_name not in {_PANEL_PROJECT_NAME, _NODE_PROJECT_NAME}:
        raise ValidationError(f"Некорректное имя Compose-проекта: {project_name}")
    command = compose_command(
        compose_path,
        *arguments,
        env_file=env_file,
    )
    command[2:2] = ["--project-name", project_name]
    return command


def _stop_failed_install_containers(
    runner: Runner,
    compose_path: Path,
    env_path: Path,
    directory: Path,
    project_name: str,
) -> str | None:
    try:
        if compose_path.is_symlink() or (
            compose_path.exists() and not compose_path.is_file()
        ):
            return f"небезопасный тип compose-файла {compose_path}"
        if not compose_path.is_file():
            return None
        if env_path.is_symlink() or not env_path.is_file():
            return f"env-файл отсутствует или имеет небезопасный тип: {env_path}"
        stopped = runner.run(
            _install_compose_command(
                compose_path,
                project_name,
                "down",
                "--remove-orphans",
                "--timeout",
                "60",
                env_file=env_path,
            ),
            cwd=directory,
            check=False,
            timeout=300,
        )
        if stopped.returncode != 0:
            return f"docker compose down завершился с кодом {stopped.returncode}"
    except BaseException as error:  # noqa: BLE001 - cleanup must survive interruption
        detail = str(error).strip()
        if len(detail) > 1000:
            detail = detail[-1000:]
        return "docker compose down: " + type(error).__name__ + (
            f": {detail}" if detail else ""
        )
    return None


def _cleanup_error_note(detail: str | None) -> str:
    if detail is None:
        return ""
    return f" Ошибка docker compose cleanup: {detail}."


def _rollback_install_resources(
    runner: Runner,
    firewall: FirewallTransaction | None,
    certificate: CertificateMaterial | None,
    containers: Sequence[str],
) -> str | None:
    if firewall is None and certificate is None:
        return None
    unsafe = _install_rollback_blockers(runner, containers)
    if unsafe:
        detail = "; ".join(unsafe)
        if certificate is not None:
            detail += "; TLS lineage и credentials сохранены"
        if firewall is not None:
            detail += (
                "; UFW не ослаблялся, снимок сохранён в "
                f"{firewall.artifact_path}"
            )
        return detail

    errors: list[str] = []
    if firewall is not None:
        try:
            firewall.rollback()
        except BaseException as error:  # noqa: BLE001 - continue independent rollback
            errors.append(
                f"UFW: {error}; ограничивающий снимок сохранён в {firewall.artifact_path}"
            )
    if certificate is not None:
        try:
            certificate.rollback(runner)
        except BaseException as error:  # noqa: BLE001 - report all rollback failures
            errors.append(f"Certbot: {error}")
    return "; ".join(errors) or None


def _install_rollback_blockers(
    runner: Runner,
    containers: Sequence[str],
) -> list[str]:
    unsafe: list[str] = []
    for container in containers:
        try:
            result = runner.run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{container}$",
                ],
                check=False,
                timeout=30,
            )
        except BaseException as error:  # noqa: BLE001 - fail closed during rollback
            unsafe.append(
                f"не удалось проверить {container}: {type(error).__name__}"
            )
            continue
        if result.returncode != 0:
            unsafe.append(f"не удалось проверить {container}")
        elif result.stdout.strip():
            unsafe.append(f"контейнер {container} всё ещё существует")
    return unsafe


def _retained_install_resources_note(
    firewall: FirewallTransaction | None,
    certificate: CertificateMaterial | None,
) -> str:
    detail = "TLS-ресурсы ещё не создавались"
    if certificate is not None:
        detail = "TLS lineage, hooks и credentials не изменялись rollback"
    if certificate is not None and certificate.transaction is None:
        detail = "скопированные TLS-файлы оставлены в каталоге установки"
    if firewall is not None:
        detail += f"; UFW не ослаблялся, снимок сохранён в {firewall.artifact_path}"
    return detail


def _restored_install_resources_note(
    firewall: FirewallTransaction | None,
    certificate: CertificateMaterial | None,
) -> str:
    restored: list[str] = []
    if firewall is not None:
        restored.append("исходное состояние UFW восстановлено")
    if certificate is not None and certificate.transaction is not None:
        restored.append("состояние Certbot восстановлено")
    elif certificate is not None:
        restored.append("скопированные TLS-файлы оставлены в каталоге установки")
    if not restored:
        return "; внешние системные ресурсы не изменялись"
    return "; " + ", ".join(restored)


def _rollback_failed_install_state(
    store: StateStore,
    directory: Path,
    role: Role,
    *,
    secrets_payload: dict[str, str] | None = None,
) -> str | None:
    inventory_path = store.paths.inventory
    secrets_path = store.paths.secrets
    remove_inventory = False
    remove_secrets = False
    errors: list[str] = []

    if inventory_path.is_symlink() or (
        inventory_path.exists() and not inventory_path.is_file()
    ):
        errors.append(f"inventory имеет небезопасный тип: {inventory_path}")
    elif inventory_path.is_file():
        try:
            current = store.load_inventory()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AttributeError,
            ValidationError,
        ) as error:
            errors.append(f"не удалось проверить созданный inventory: {error}")
        else:
            if current.role != role or Path(current.install_dir) != directory:
                errors.append("inventory больше не соответствует текущей попытке установки")
            else:
                remove_inventory = True

    if secrets_path.is_symlink() or (secrets_path.exists() and not secrets_path.is_file()):
        errors.append(f"secrets имеет небезопасный тип: {secrets_path}")
    elif secrets_path.is_file():
        if secrets_payload is None:
            errors.append("появился не ожидавшийся файл secrets")
        else:
            try:
                saved_secrets = store.load_secrets()
            except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
                errors.append(f"не удалось проверить созданный secrets: {error}")
            else:
                if saved_secrets != secrets_payload:
                    errors.append("secrets больше не соответствует текущей попытке установки")
                else:
                    remove_secrets = True

    if errors:
        return "; ".join(errors)
    try:
        # Inventory is the manager commit marker; keep secrets if it cannot be removed.
        if remove_inventory:
            inventory_path.unlink()
        if remove_secrets:
            secrets_path.unlink()
    except OSError as error:
        return f"не удалось удалить созданный manager state: {error}"
    return None


def _rollback_failed_install_state_safely(
    store: StateStore,
    directory: Path,
    role: Role,
    *,
    secrets_payload: dict[str, str] | None = None,
) -> str | None:
    try:
        return _rollback_failed_install_state(
            store,
            directory,
            role,
            secrets_payload=secrets_payload,
        )
    except BaseException as error:  # noqa: BLE001 - retain resources on uncertain state
        return (
            "аварийная ошибка проверки manager state: "
            f"{type(error).__name__}: {str(error).strip() or 'без описания'}"
        )


def _preflight(
    runner: Runner,
    store: StateStore,
    directory: Path,
    *,
    expected: Path,
    role: Role,
) -> None:
    require_root()
    require_ubuntu_2404()
    if directory != expected:
        raise ValidationError(f"Для этой роли разрешён только каталог {expected}.")
    TransactionJournal.ensure_available(store)
    if store.paths.inventory.exists() or store.paths.inventory.is_symlink():
        raise ValidationError("Сервер уже принят под управление; чистая установка запрещена.")
    if store.paths.secrets.exists() or store.paths.secrets.is_symlink():
        raise ValidationError(
            "Обнаружен secrets.json без активного inventory. "
            "Проверьте незавершённую установку или архивируйте файл вручную."
        )
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValidationError(f"Каталог {directory} имеет небезопасный тип.")
    for command in ("docker", "openssl", "curl", "sshd"):
        if not command_exists(command):
            raise ValidationError(
                f"Команда {command} не найдена. Сначала выполните корневой install.sh."
            )
    _require_local_docker(runner)
    runner.run(["docker", "compose", "version"], timeout=30)
    if (
        directory.exists()
        and any(directory.iterdir())
        and _archive_incomplete_install(runner, directory, role) is None
    ):
        raise ValidationError(
            f"Каталог {directory} не пуст. Используйте adoption, а не чистую установку."
        )


def _require_local_docker(runner: Runner) -> None:
    local_endpoints = {
        "unix:///run/docker.sock",
        "unix:///var/run/docker.sock",
    }
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host and docker_host not in local_endpoints:
        raise ValidationError(
            "Чистая установка запрещена через удалённый DOCKER_HOST. "
            "Используйте локальный rootful Docker socket."
        )
    docker_context = os.environ.get("DOCKER_CONTEXT", "").strip()
    if docker_context and docker_context != "default":
        raise ValidationError(
            "Чистая установка запрещена через нестандартный DOCKER_CONTEXT. "
            "Переключитесь на локальный context default."
        )
    inspected = runner.run(
        [
            "docker",
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ],
        timeout=30,
    )
    endpoint = inspected.stdout.strip()
    if endpoint not in local_endpoints:
        raise ValidationError(
            "Активный Docker context не использует локальный rootful socket: "
            f"{endpoint or 'endpoint не определён'}."
        )


def _install_marker(directory: Path) -> Path:
    return directory / _INSTALL_MARKER_NAME


def _start_install_attempt(directory: Path, role: Role) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError(f"Каталог {directory} имеет небезопасный тип.")
    marker = _install_marker(directory)
    payload = (
        json.dumps(
            {
                "schema_version": _INSTALL_MARKER_SCHEMA,
                "role": role,
                "state": "installing",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as error:
        raise ValidationError(f"Маркер установки уже существует: {marker}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(marker, 0o600)
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        marker.unlink(missing_ok=True)
        raise


def _load_install_marker(directory: Path) -> dict[str, object] | None:
    marker = _install_marker(directory)
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValidationError(f"Маркер установки имеет небезопасный тип: {marker}")
    if not marker.is_file():
        return None
    try:
        snapshot = read_stable_regular_file(
            marker,
            max_size=_MAX_INSTALL_MARKER_SIZE,
            label="Маркер установки",
        )
        if os.name == "posix" and (
            snapshot.uid != os.geteuid() or snapshot.mode & 0o077
        ):
            raise ValidationError(
                f"Маркер установки должен принадлежать root и иметь приватные права: {marker}"
            )
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"Маркер установки повреждён: {marker}") from error
    if not isinstance(payload, dict):
        raise ValidationError(f"Маркер установки повреждён: {marker}")
    return payload


def _validated_install_marker(directory: Path, role: Role) -> dict[str, object] | None:
    payload = _load_install_marker(directory)
    if payload is None:
        return None
    if payload != {
        "schema_version": _INSTALL_MARKER_SCHEMA,
        "role": role,
        "state": "installing",
    }:
        raise ValidationError(
            f"Маркер {_install_marker(directory)} не подтверждает незавершённую установку {role}."
        )
    return payload


def _archive_incomplete_install(
    runner: Runner,
    directory: Path,
    role: Role,
) -> Path | None:
    if _validated_install_marker(directory, role) is None:
        return None
    compose_path = directory / "docker-compose.yml"
    env_path = directory / ".env"
    for path in (compose_path, env_path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValidationError(f"Файл незавершённой установки имеет небезопасный тип: {path}")
    if compose_path.is_file():
        project_name = (
            _PANEL_PROJECT_NAME if role == "panel" else _NODE_PROJECT_NAME
        )
        command = _install_compose_command(
            compose_path,
            project_name,
            "down",
            "--remove-orphans",
            "--timeout",
            "60",
            env_file=env_path if env_path.is_file() else None,
        )
        runner.run(
            command,
            cwd=directory,
            timeout=300,
        )
    suffix = f"{int(time.time())}-{secrets.token_hex(4)}"
    archived = directory.with_name(f"{directory.name}.incomplete-{suffix}")
    if archived.exists() or archived.is_symlink():
        raise ValidationError(f"Архивный путь уже занят: {archived}")
    directory.rename(archived)
    return archived


def _finish_install_attempt(directory: Path, role: Role) -> None:
    if _validated_install_marker(directory, role) is None:
        raise TransactionError("Маркер текущей установки неожиданно исчез.")
    _install_marker(directory).unlink()


def _ensure_container_names_available(runner: Runner, names: Sequence[str]) -> None:
    listed = runner.run(
        ["docker", "ps", "--all", "--format", "{{.Names}}"],
        timeout=30,
    )
    existing = {line.strip() for line in listed.stdout.splitlines() if line.strip()}
    occupied = [name for name in names if name in existing]
    if occupied:
        raise ValidationError(
            "Уже существуют Docker-контейнеры с зарезервированными именами: "
            + ", ".join(occupied)
            + ". Используйте adoption."
        )


def _pull_component_image(runner: Runner, component: str, registry: str) -> str:
    return pull_verified(runner, component, registry)


def _pull_base_images(runner: Runner, images: Sequence[str]) -> None:
    for image in images:
        _pinned_image(image)
        result = runner.run(["docker", "pull", image], check=False, timeout=1800)
        if result.returncode != 0:
            raise TransactionError(
                f"Не удалось скачать фиксированный образ {image}. "
                "Для Docker Hub выполните rwm registry login --registry docker-hub."
            )


def _prepare_panel_directories(runner: Runner, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    postgres = directory / "data" / "postgres"
    postgres.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    os.chmod(postgres, 0o700)
    runner.run(["chown", "999:999", str(postgres)])


def _prepare_node_directories(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "site").mkdir(mode=0o755)
    (directory / "logs").mkdir(mode=0o700)
    os.chmod(directory, 0o700)


def _validate_site_source(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValidationError("Каталог выбранного маскировочного сайта не найден.")
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise ValidationError("Не удалось безопасно разрешить каталог сайта.") from error
    if os.name == "posix":
        effective_uid = os.geteuid()
        trusted_uids = {0, effective_uid}
        for directory in (resolved, *resolved.parents):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in trusted_uids
                or (
                    stat.S_IMODE(info.st_mode) & 0o022
                    and not (
                        directory != resolved
                        and info.st_uid == 0
                        and stat.S_IMODE(info.st_mode) & stat.S_ISVTX
                    )
                )
            ):
                raise ValidationError(
                    "Каталог сайта и все его родители должны принадлежать root "
                    "и не допускать group/world write."
                )
    index = source / "index.html"
    if not index.is_file() or index.is_symlink():
        raise ValidationError("В шаблоне маскировочного сайта отсутствует index.html.")


def _install_static_site(source: Path, target: Path) -> None:
    total_size = 0
    file_count = 0
    for item in sorted(source.rglob("*"), key=str):
        if item.is_symlink():
            raise ValidationError(f"Символические ссылки в шаблоне запрещены: {item}")
        if os.name == "posix":
            info = item.lstat()
            if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValidationError(
                    f"Элемент шаблона должен принадлежать root и не допускать запись не-root: {item}"
                )
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        if not item.is_file():
            raise ValidationError(f"Неподдерживаемый элемент шаблона: {item}")
        file_count += 1
        if file_count > 5000:
            raise ValidationError("Шаблон маскировочного сайта слишком большой.")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        atomic_copy(item, destination, mode=0o644)
        total_size += destination.stat().st_size
        if total_size > 100 * 1024 * 1024:
            raise ValidationError("Шаблон маскировочного сайта слишком большой.")


def _wait_api_ready(api: RemnawaveApi, *, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = api.auth_status()
            if status.get("isRegisterAllowed") is True:
                return
            raise TransactionError("Свежая Panel неожиданно не разрешает регистрацию.")
        except TransactionError as error:
            last_error = error
            time.sleep(3)
    raise TransactionError(f"Panel API не подготовилась к регистрации: {last_error}")


def _admin_credentials(username: str | None, password: str | None) -> tuple[str, str]:
    selected_username = (
        "admin_" + secrets.token_hex(5) if username is None else username
    )
    selected_password = (
        "Aa7_" + secrets.token_urlsafe(30) if password is None else password
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", selected_username):
        raise ValidationError("Имя администратора: 3-32 латинских букв, цифр, _ или -.")
    validate_admin_password(selected_password)
    return selected_username, selected_password


def _component(name: str, container: str) -> Component:
    return Component(name=name, service=container, container=container)


def _certificate_secret_paths(
    directory: Path,
    spec: CertificateSpec,
    material: CertificateMaterial,
) -> list[Path]:
    if spec.method == "existing":
        return [material.host_root / "fullchain.pem", material.host_root / "privkey.pem"]
    if spec.method in {"cloudflare", "gcore"}:
        if material.credentials_file is None:
            raise TransactionError(
                "Certbot не вернул путь DNS credential; регистрация managed-файла остановлена."
            )
        return [material.credentials_file]
    return []


def _add_managed_files(
    store: StateStore,
    inventory: Inventory,
    paths: Sequence[Path],
    *,
    kind: str,
) -> None:
    existing = {Path(item.path).resolve() for item in inventory.managed_files}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise TransactionError(
                f"Ожидаемый managed-файл отсутствует или имеет небезопасный тип: {path}"
            )
        resolved = path.resolve()
        if resolved in existing:
            continue
        inventory.managed_files.append(
            ManagedFile(path=str(resolved), sha256=sha256_file(path), kind=kind)
        )
        existing.add(resolved)
    store.save_inventory(inventory)


def _dotenv_value(value: str) -> str:
    selected = _secret_value(value, "значение env")
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", selected):
        return selected
    if "'" in selected:
        raise ValidationError("Значение env содержит недопустимую одинарную кавычку.")
    return "'" + selected + "'"


def _secret_value(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValidationError(f"{label} пуст или имеет недопустимую длину.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(f"{label} содержит управляющие символы.")
    return value


def _hex_secret(value: str, length: int, label: str) -> None:
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValidationError(f"{label} должен содержать {length} шестнадцатеричных символов.")


def _pinned_image(image: str) -> None:
    if not re.fullmatch(
        r"[A-Za-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}",
        image,
    ):
        raise ValidationError(f"Некорректный Docker image: {image}")


def _yaml_string(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValidationError("Некорректное значение для compose.")
    return json.dumps(value, ensure_ascii=True)


def _compose_mount_lines(mounts: tuple[str, ...]) -> str:
    if not mounts:
        raise ValidationError("Для сертификата не сформированы bind mounts.")
    return "\n".join(f"      - {_yaml_string(mount)}" for mount in mounts)


def _nginx_path(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
        raise ValidationError("Некорректный путь TLS внутри nginx-контейнера.")
    return value
