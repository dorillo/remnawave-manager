"""Inspect, test and transactionally change the disguise site's source IPv4."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import urlsplit

from .image_proxy import upstream_url as image_upstream
from .loop_proxy import MEDIA_PATTERN
from .fokus_proxy import upstream as fokus_upstream

from .backup import create_backup
from .compose import inspect_compose
from .disguise import _managed_nginx_configs, _site_roots, _target, _validate_configuration
from .errors import TransactionError, ValidationError
from .models import Inventory
from .nginx import activate_nginx_config, nginx_is_running, _structural_text
from .runner import Runner, atomic_write_text, read_stable_regular_file, sanitize_external_text, sha256_file
from .site_egress_config import configure_egress, site_servers, validate_source
from .state import StateStore

ConfigSnapshots = list[tuple[Path, str, int]]

@dataclass(frozen=True)
class SourceAddress:
    address: str
    interface: str


@dataclass(frozen=True)
class Probe:
    source: str
    url: str
    ok: bool
    http_status: int
    local_ip: str
    remote_ip: str
    seconds: float
    detail: str
    via: str = "direct"
    samples: tuple[tuple[str, str], ...] = ()


# Only anonymous, read-only requests used by the shipped sites. The second
# Morrow page catches the partial failure that motivated this feature.
PROBES = {
    "01-northline": (("https://mastodon.ml/api/v1/directory?local=true&order=active&limit=20", "/_northline/mastodon/api/v1/directory?local=true&order=active&limit=20"),),
    "02-aster-observatory": (("https://rutube.ru/api/search/video/?query=nature&page=1", "/_aster/rutube-search?query=nature&page=1"),),
    "03-morrow-coffee": tuple((f"https://yappy.media/api/feed?page={page}&fingerprint=", f"/_morrow/yappy/feed?page={page}") for page in (1, 2)),
    "04-signal-works": (("https://otvet.mail.ru/api/topic/feed?limit=20", "/_answers/mail/feed?limit=20"),),
    "05-field-notes": (("https://ru.wikipedia.org/w/api.php?format=json&action=query&list=random&rnnamespace=0&rnlimit=1", "/_svod/wikipedia/random"),),
    "06-loop-archive": (("https://gifs.ru/api/v1/Popular?contentType=1&skip=0&take=24", "/_loop/gifs/popular?contentType=1&skip=0&take=24"),),
    "07-fokus-news": (("https://ria.ru/export/rss2/archive/index.xml", "/_fokus/ria/feed"),),
}


def source_addresses(runner: Runner) -> list[SourceAddress]:
    result = runner.run(["ip", "-j", "-4", "address", "show", "scope", "global"])
    try:
        interfaces = json.loads(result.stdout)
        addresses = []
        for item in interfaces:
            name = item["ifname"]
            # Tunnel and container addresses are not public egress candidates.
            if re.match(r"^(?:lo$|docker|br-|veth|warp|wg|tun|tap)", name) or "UP" not in item.get("flags", []):
                continue
            for info in item.get("addr_info", []):
                if info.get("family") != "inet" or info.get("scope") != "global":
                    continue
                if info.get("tentative") or info.get("dadfailed") or info.get("valid_life_time") == 0:
                    continue
                addresses.append(SourceAddress(validate_source(info["local"]), name))
        return sorted(set(addresses), key=lambda x: (x.interface, int(ipaddress.IPv4Address(x.address))))
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError("Не удалось прочитать IPv4 сетевых интерфейсов.") from error


def _assigned_source(runner: Runner, source: str | None) -> None:
    if source is not None and source not in {item.address for item in source_addresses(runner)}:
        raise ValidationError(f"IPv4 {source} не назначен активному внешнему интерфейсу сервера.")


def _context(runner: Runner, store: StateStore) -> tuple[Inventory, Path, dict, set[str], ConfigSnapshots]:
    inventory = store.load_inventory()
    if inventory.role != "node" or "nginx" not in inventory.components:
        raise ValidationError("Исходящий IP сайта настраивается для node с управляемым nginx.")
    _validate_configuration(inventory)
    target = _target(inventory)
    compose = inspect_compose(runner, Path(inventory.compose_file), Path(inventory.env_file) if inventory.env_file else None)
    service = compose["services"].get(inventory.components["nginx"].service, {})
    if service.get("network_mode") != "host":
        raise ValidationError("Выбор IP сайта поддерживает nginx с network_mode: host; сеть контейнера не будет изменена.")
    roots = _site_roots(runner, inventory, target)
    snapshots = _managed_nginx_configs(inventory)
    return inventory, target, service, roots, snapshots


def _current(snapshots: ConfigSnapshots, roots: set[str]) -> tuple[str | None, int]:
    values = []
    routes = 0
    for _, text, _ in snapshots:
        _, count, previous = configure_egress(text, roots, None)
        routes += count
        values.extend(previous)
    if not routes:
        raise ValidationError("Не найдены известные внешние маршруты сайта; сначала примените штатный шаблон.")
    if len(set(values)) != 1:
        raise ValidationError("У виртуальных хостов сайта разные исходящие IP; требуется согласовать конфигурацию.")
    return values[0], routes


def egress_status(runner: Runner, store: StateStore) -> dict:
    _, _, _, roots, snapshots = _context(runner, store)
    source, routes = _current(snapshots, roots)
    addresses = source_addresses(runner)
    return {"source": source or "auto", "available": source is None or source in {x.address for x in addresses},
            "addresses": [asdict(x) for x in addresses], "proxy_routes": routes}


def configured_egress_health(runner: Runner, store: StateStore, inventory: Inventory) -> dict | None:
    """No network requests; installations without this feature stay unaffected."""
    if inventory.role != "node" or "nginx" not in inventory.components or not inventory.site_dirs:
        return None
    if not any("# rwm-site-egress" in text for _, text, _ in _managed_nginx_configs(inventory)):
        return None
    return egress_status(runner, store)


def _template_probes(target: Path) -> tuple[tuple[str, str], ...]:
    try:
        marker = read_stable_regular_file(target / ".rwm-template.json", max_size=4096, label="Описание шаблона")
        template = json.loads(marker.data)["template"]
        return PROBES[template]
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise ValidationError("Не удалось определить шаблон для проверки источника; примените штатный шаблон сайта.") from error


def _media_route(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return None
    route = "/_images/" + parsed.netloc + parsed.path + ("?" + parsed.query if parsed.query else "")
    if image_upstream(route) == url:
        return route
    if parsed.netloc == "media.gifs.ru" and not parsed.query and re.fullmatch("/" + MEDIA_PATTERN, parsed.path):
        return "/_loop/media" + parsed.path
    route = "/_fokus/media" + parsed.path
    if fokus_upstream(route, parsed.query) == url:
        return route
    return None


def _sample_routes(value: object, url: str) -> tuple[tuple[str, str], ...]:
    result = []
    if "/api/v1/directory" in url and isinstance(value, list) and value:
        identity = str(value[0].get("id", "")) if isinstance(value[0], dict) else ""
        if re.fullmatch(r"[0-9]{1,20}", identity):
            path = f"/api/v1/accounts/{identity}/statuses?limit=20&exclude_replies=true&exclude_reblogs=true"
            result.append(("https://mastodon.ml" + path, "/_northline/mastodon" + path))
    if ("/api/v1/Popular" in url or "/_loop/gifs/popular" in url) and isinstance(value, dict):
        rows = value.get("result")
        identity = str(rows[0].get("id", "")) if isinstance(rows, list) and rows and isinstance(rows[0], dict) else ""
        if re.fullmatch(r"[A-Za-z0-9]{1,12}", identity):
            result.append(("https://gifs.ru/api/v1/File/FileById/" + identity, "/_loop/gifs/item/" + identity))
    # Select a bounded, allowlisted image sample, never an arbitrary API-provided URL.
    def strings(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, str):
            yield node
        elif isinstance(node, list):
            for item in node[:3]:
                yield from strings(item, depth + 1)
        elif isinstance(node, dict):
            if isinstance(node.get("cloudSource300"), str):
                yield node["cloudSource300"]
            for item in list(node.values())[:40]:
                yield from strings(item, depth + 1)
    for index, sample in enumerate(strings(value)):
        if index >= 300:
            break
        try:
            route = _media_route(sample)
        except ValueError:
            continue
        if route:
            result.append((sample, route))
            break
    return tuple(result)


def _probe(runner: Runner, url: str, source: str | None, extra: tuple[str, ...] = ()) -> Probe:
    command = ["curl", "-q", "--noproxy", "*", "-sS", "--connect-timeout", "5", "--max-time", "18",
               "--max-filesize", "4194304", "--proto", "=http,https", "-A", "Mozilla/5.0 (compatible; MorrowVideo/1.0)",
               "-H", "Accept: application/json", "-w", "\nRWM_EGRESS_META:%{json}"]
    media = urlsplit(url).path.startswith(("/_images/", "/_loop/media/", "/_fokus/media/")) or _media_route(url) is not None
    if media:
        command += ["--range", "0-1023", "--output", "/dev/null"]
    if "--unix-socket" not in extra:
        command.append("-4")
    if source:
        command += ["--interface", source]
    command += [*extra, url]
    result = runner.run(command, check=False, timeout=25)
    body, _, metadata = result.stdout.rpartition("\nRWM_EGRESS_META:")
    try:
        info = json.loads(metadata)
        if not isinstance(info, dict):
            raise ValueError("Invalid curl metadata")
        status = int(info.get("http_code", 0))
        elapsed = float(info.get("time_total", 0))
    except (ValueError, TypeError):
        info, status, elapsed = {}, 0, 0.0
    ok = result.returncode == 0 and 200 <= status < 300
    detail = "OK" if ok else f"curl={result.returncode}, HTTP={status}"
    if result.returncode and result.stderr:
        detail += ": " + sanitize_external_text(result.stderr, limit=600)
    samples = ()
    if ok and not media:
        try:
            if "ria.ru/" in url or "/_fokus/" in url:
                xml = ElementTree.fromstring(body)
                value = [item.attrib for item in xml.iter("enclosure")]
            else:
                value = json.loads(body)
                if not isinstance(value, (dict, list)):
                    raise ValueError("not structured data")
            samples = _sample_routes(value, url)
        except (ValueError, ElementTree.ParseError):
            ok, detail = False, "Источник вернул неожиданный формат данных"
    if ok and source and info.get("local_ip") != source:
        ok, detail = False, "Исходящий локальный IP не совпал с выбранным"
    return Probe(source or "auto", url, ok, status, info.get("local_ip", ""), info.get("remote_ip", ""), elapsed, detail, "nginx" if extra else "direct", samples)


def check_egress(runner: Runner, store: StateStore, source: str | None) -> list[Probe]:
    if source is not None:
        source = validate_source(source)
    inventory, target, service, roots, snapshots = _context(runner, store)
    _assigned_source(runner, source)
    probes = _template_probes(target)
    results = [_probe(runner, url, source) for url, _ in probes]
    current, _ = _current(snapshots, roots)
    if source == current:
        if not nginx_is_running(runner, inventory):
            results.append(Probe(source or "auto", "nginx", False, 0, "", "", 0, "nginx не запущен", "nginx"))
        else:
            results.extend(_live_results(runner, _local_target(snapshots, roots, service), probes))
    return results


def _local_target(snapshots: ConfigSnapshots, roots: set[str], service: dict) -> tuple[str, tuple[str, ...]]:
    """Connect directly to this nginx listener, bypassing public DNS and Xray."""
    for _, text, _ in snapshots:
        for opening, closing in site_servers(text, roots):
            body = text[opening + 1:closing]
            structure = _structural_text(body)
            name = re.search(r"\bserver_name\s+([a-zA-Z0-9][a-zA-Z0-9.-]*)(?:\s|;)", structure)
            host = name[1] if name else "localhost"
            for match in re.finditer(r"\blisten\s+([^;]+);", structure):
                options = match[1].split()
                address = options[0]
                scheme = "https" if "ssl" in options else "http"
                extra = ["--insecure"] if scheme == "https" else []
                if "proxy_protocol" in options:
                    extra.append("--haproxy-protocol")
                if address.startswith("unix:"):
                    socket = address[5:]
                    mapped = []
                    for volume in service.get("volumes", []):
                        if volume.get("type") == "bind":
                            try:
                                relative = Path(socket).relative_to(volume["target"])
                                mapped.append((len(volume["target"]), str(Path(volume["source"]) / relative)))
                            except ValueError:
                                pass
                    if not mapped:
                        continue
                    extra += ["--unix-socket", max(mapped)[1]]
                    return f"{scheme}://{host}", tuple(extra)
                if not re.fullmatch(r"(?:(?:[0-9.]+|\*):)?[0-9]+", address):
                    continue
                port = address.rsplit(":", 1)[-1]
                ip = address.rsplit(":", 1)[0] if ":" in address else "127.0.0.1"
                if ip in {"*", "0.0.0.0"}:
                    ip = "127.0.0.1"
                extra += ["--resolve", f"{host}:{port}:{ip}"]
                return f"{scheme}://{host}:{port}", tuple(extra)
    raise ValidationError("Не найден локальный listener nginx для проверки сайта.")


def _live_results(runner: Runner, target: tuple[str, tuple[str, ...]], probes: tuple[tuple[str, str], ...]) -> list[Probe]:
    origin, extra = target
    results = []
    samples = []
    for _, route in probes:
        # Yappy already has two distinct feed probes. Keep its previous request
        # budget: duplicating each page can trigger the provider's anti-bot rules.
        attempts = 1 if route.startswith('/_morrow/yappy/') else 3
        for attempt in range(attempts):
            result = _probe(runner, origin + route, None, extra)
            results.append(result)
            if result.http_status in (403, 429):
                return results
            if attempt == 0 and result.ok:
                samples.extend(result.samples)
    for _, route in dict.fromkeys(samples):
        for _ in range(3):
            result = _probe(runner, origin + route, None, extra)
            results.append(result)
            if result.http_status in (403, 429):
                return results
    return results


def _check_live(runner: Runner, target: tuple[str, tuple[str, ...]] | None, probes: tuple[tuple[str, str], ...]) -> None:
    if target is None:
        return
    for result in _live_results(runner, target, probes):
        if not result.ok:
            raise TransactionError(f"Проверка сайта через nginx не прошла: {result.url}: {result.detail}")


def set_egress(runner: Runner, store: StateStore, source: str | None, *, skip_check: bool = False) -> dict:
    if source is not None:
        source = validate_source(source)
    inventory, target, service, roots, snapshots = _context(runner, store)
    old_source, routes = _current(snapshots, roots)
    _assigned_source(runner, source)
    probes = _template_probes(target) if not skip_check else ()
    running = nginx_is_running(runner, inventory)
    local_target = None
    if not skip_check:
        if not running:
            raise ValidationError("Для проверки сайта запустите nginx или явно используйте --skip-check.")
        local_target = _local_target(snapshots, roots, service)
        for url, _ in probes:
            result = _probe(runner, url, source)
            if not result.ok:
                raise ValidationError(f"Источник недоступен через {source or 'системный маршрут'}: {url}: {result.detail}. Настройки не изменены.")
    changes = []
    for path, text, mode in snapshots:
        updated, _, _ = configure_egress(text, roots, source)
        if updated != text:
            changes.append((path, text, updated, mode))
    if not changes:
        _check_live(runner, local_target, probes)
        return {"source": source or "auto", "previous": old_source or "auto", "changed": False, "proxy_routes": routes, "checked": not skip_check}
    backup = create_backup(runner, store, reason="pre-site-egress", retention=None)
    # Re-read after slow backup/probes: never overwrite a concurrent edit.
    _validate_configuration(inventory)
    if _managed_nginx_configs(inventory) != snapshots or _site_roots(runner, inventory, target) != roots:
        raise ValidationError("Конфигурация сайта изменилась во время подготовки; повторите операцию.")
    _assigned_source(runner, source)
    saved_inventory = Inventory.from_dict(inventory.to_dict())
    attempted = []
    inventory_attempted = False
    try:
        for path, old, new, mode in changes:
            attempted.append((path, old, new, mode))
            atomic_write_text(path, new, mode=mode)
        activate_nginx_config(runner, inventory, was_running=running)
        _check_live(runner, local_target, probes)
        for item in inventory.managed_files:
            if any(Path(item.path) == change[0] for change in changes):
                item.sha256 = sha256_file(Path(item.path))
        inventory_attempted = True
        store.save_inventory(inventory)
    except BaseException as error:
        rollback_errors = []
        for path, old, new, mode in reversed(attempted):
            try:
                current = read_stable_regular_file(path, max_size=4 * 1024 * 1024, label="Конфигурация nginx")
                if current.data.decode("utf-8") not in {old, new} or current.mode != mode:
                    raise TransactionError("Конфигурация изменена внешним процессом")
                atomic_write_text(path, old, mode=mode)
            except BaseException as rollback:
                rollback_errors.append(f"{path}: {rollback}")
        if attempted and not rollback_errors:
            try:
                activate_nginx_config(runner, saved_inventory, was_running=running)
            except BaseException as rollback:
                rollback_errors.append(f"nginx: {rollback}")
        if inventory_attempted:
            try:
                store.save_inventory(saved_inventory)
            except BaseException as rollback:
                rollback_errors.append(f"inventory: {rollback}")
        if rollback_errors:
            raise TransactionError("Смена IP не выполнена, rollback неполон: " + "; ".join(rollback_errors) + f". Причина: {error}") from error
        raise TransactionError(f"Смена IP не выполнена; прежняя конфигурация восстановлена. Причина: {error}") from error
    return {"source": source or "auto", "previous": old_source or "auto", "changed": True,
            "proxy_routes": routes, "checked": not skip_check, "backup": str(backup.path)}
