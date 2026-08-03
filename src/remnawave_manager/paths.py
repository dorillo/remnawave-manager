from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path = Path("/")

    def _under_root(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def etc(self) -> Path:
        return self._under_root("/etc/remnawave-manager")

    @property
    def state(self) -> Path:
        return self._under_root("/var/lib/remnawave-manager")

    @property
    def backups(self) -> Path:
        return self._under_root("/var/backups/remnawave-manager")

    @property
    def logs(self) -> Path:
        return self._under_root("/var/log/remnawave-manager")

    @property
    def lock(self) -> Path:
        return self._under_root("/run/remnawave-manager/manager.lock")

    @property
    def inventory(self) -> Path:
        return self.state / "inventory.json"

    @property
    def settings(self) -> Path:
        return self.etc / "settings.json"

    @property
    def secrets(self) -> Path:
        return self.etc / "secrets.json"
