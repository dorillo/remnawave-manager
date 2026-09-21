"""Observe actual source addresses through a real local nginx (no Internet)."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import ProxyHandler, build_opener

from remnawave_manager.backup import BackupResult
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import Runner, sha256_file
from remnawave_manager.site_config import upgrade_site_config
from remnawave_manager.site_egress import SourceAddress, _probe, set_egress
from remnawave_manager.site_egress_config import validate_source


def local_source():
    """Find a non-loopback address that can reach the local fixture; no aliases."""
    if shutil.which("ip"):
        result = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=True)
        candidates = [a["local"] for i in json.loads(result.stdout) for a in i.get("addr_info", []) if a.get("family") == "inet"]
    elif shutil.which("ifconfig"):
        result = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True, check=True)
        candidates = re.findall(r"\binet ([0-9.]+)", result.stdout)
    else:
        raise unittest.SkipTest("No local interface discovery tool")
    for candidate in candidates:
        try:
            validate_source(candidate)
            with socket.socket() as listener, socket.socket() as client:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                client.settimeout(.3)
                client.bind((candidate, 0))
                client.connect(listener.getsockname())
                connection, _ = listener.accept()
                connection.close()
                return candidate
        except (OSError, ValueError, ValidationError):
            pass
    raise unittest.SkipTest("No usable local non-loopback IPv4")


@unittest.skipUnless(shutil.which("nginx") and shutil.which("curl"), "nginx and curl are required")
class EgressNginxTests(unittest.TestCase):
    def test_source_selection_reset_reload_template_change_and_live_rollback(self):
        self.exercise(False)

    def test_unix_listener_with_proxy_protocol_used_by_reality_nodes(self):
        self.exercise(True)

    def exercise(self, unix_listener):
        source = local_source()
        observed = []
        reject_selected = False

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.client_address[0], self.path))
                status = 503 if reject_selected and self.client_address[0] == source and self.headers.get("X-Egress-Test") else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"results": [], "source": self.client_address[0]}).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        process = None
        socket_directory = tempfile.TemporaryDirectory(prefix="rwm-", dir=Path("/tmp").resolve())
        self.addCleanup(socket_directory.cleanup)
        socket_path = str(Path(socket_directory.name) / "nginx.sock")
        service = {"network_mode": "host", "volumes": [{"type": "bind", "source": socket_directory.name, "target": socket_directory.name}]}

        def read_response(path):
            if unix_listener:
                result = subprocess.run(["curl", "-q", "--noproxy", "*", "-sS", "--max-time", "3", "--unix-socket", socket_path,
                                         "--haproxy-protocol", "http://localhost" + path], capture_output=True, check=True)
                return result.stdout
            with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
                return response.read()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            site = root / "site"
            site.mkdir()
            (site / ".rwm-template.json").write_text(json.dumps({"template": "03-morrow-coffee"}))
            production = root / "site.conf"
            runtime = root / "runtime.conf"
            roots = {str(site)}
            listener = f"unix:{socket_path} proxy_protocol" if unix_listener else f"127.0.0.1:{port}"
            original = f'''server {{
    listen {listener};
    server_name localhost;
    root {site};
    location /transport-control {{ proxy_pass http://127.0.0.1:{upstream.server_port}; }}
}}
'''
            production.write_text(upgrade_site_config(original, "03-morrow-coffee", roots)[0])
            production.chmod(0o600)
            inventory = Inventory(schema_version=1, role="node", install_dir=str(root), compose_file=str(root / "compose.yml"),
                                  env_file=None, webserver="nginx", nginx_files=[str(production)], site_dirs=[str(site)],
                                  components={"nginx": Component("nginx", "nginx")},
                                  managed_files=[ManagedFile(str(production), sha256_file(production), "nginx")])
            store = mock.Mock()
            generation = 0

            def activate(*_, **__):
                nonlocal process, generation
                generation += 1
                # Change only the upstream transport for this offline fixture.
                # Source bindings, rewrite rules and all manager transactions are real.
                text = re.sub(r"proxy_pass https://[^/;]+", f"proxy_set_header X-Egress-Test nginx;\n        proxy_pass http://127.0.0.1:{upstream.server_port}", production.read_text())
                text = re.sub(r"^.*proxy_ssl_.*\n", "", text, flags=re.M)
                text = text.replace(f"root {site};", f"root {site};\n    location = /ready {{ return 200 '{generation}'; }}")
                runtime.write_text(f"pid {root}/nginx.pid;\nerror_log {root}/error.log;\nevents {{}}\nhttp {{ access_log off;\n{text}\n}}")
                result = subprocess.run(["nginx", "-p", str(root), "-c", str(runtime), "-t"], capture_output=True, text=True)
                if result.returncode:
                    raise AssertionError(result.stderr)
                # Production activation recreates the nginx container because
                # its config files are bind-mounted. Model that process boundary.
                if process is not None:
                    process.terminate()
                    process.wait(timeout=5)
                process = subprocess.Popen(["nginx", "-p", str(root), "-c", str(runtime), "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(80):
                    try:
                        if read_response("/ready") == str(generation).encode():
                            return
                    except (OSError, subprocess.CalledProcessError):
                        pass
                    time.sleep(.025)
                raise AssertionError("nginx did not load the new configuration")

            def snapshots():
                return [(production, production.read_text(), 0o600)]

            def redirected_probe(runner, url, address, extra=()):
                if url.startswith("https://yappy.media/"):
                    url = url.replace("https://yappy.media", f"http://127.0.0.1:{upstream.server_port}")
                return _probe(runner, url, address, extra)

            def request_source(path="/_morrow/yappy/feed?page=2"):
                return json.loads(read_response(path))["source"]

            try:
                activate()
                self.assertEqual(request_source(), "127.0.0.1")
                with (
                    mock.patch("remnawave_manager.site_egress._context", side_effect=lambda *_: (inventory, site, service, roots, snapshots())),
                    mock.patch("remnawave_manager.site_egress._site_roots", return_value=roots),
                    mock.patch("remnawave_manager.site_egress.source_addresses", return_value=[SourceAddress(source, "fixture")]),
                    mock.patch("remnawave_manager.site_egress.nginx_is_running", return_value=True),
                    mock.patch("remnawave_manager.site_egress.create_backup", return_value=BackupResult(root / "backup", {})),
                    mock.patch("remnawave_manager.site_egress.activate_nginx_config", side_effect=activate),
                    mock.patch("remnawave_manager.site_egress._probe", side_effect=redirected_probe),
                ):
                    result = set_egress(Runner(), store, source)
                    self.assertTrue(result["checked"])
                    self.assertEqual(request_source(), source)
                    self.assertEqual(request_source("/transport-control"), "127.0.0.1")
                    # Template replacement must carry the binding to new routes too.
                    production.write_text(upgrade_site_config(production.read_text(), "02-aster-observatory", roots)[0])
                    inventory.managed_files[0].sha256 = sha256_file(production)
                    activate()
                    self.assertEqual(request_source("/_aster/rutube-search?query=test&page=1"), source)
                    # Re-read persisted configuration after a complete nginx restart.
                    process.terminate()
                    process.wait(timeout=5)
                    process = None
                    activate()
                    self.assertEqual(request_source(), source)
                    set_egress(Runner(), store, None)
                    self.assertEqual(request_source(), "127.0.0.1")
                    previous = production.read_bytes()
                    reject_selected = True
                    with self.assertRaisesRegex(TransactionError, "прежняя конфигурация восстановлена"):
                        set_egress(Runner(), store, source)
                    self.assertEqual(production.read_bytes(), previous)
                    self.assertEqual(request_source(), "127.0.0.1")
                    self.assertEqual(inventory.managed_files[0].sha256, sha256_file(production))
            finally:
                if process is not None:
                    process.terminate()
                    process.wait(timeout=5)
