from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .errors import TransactionError, ValidationError
from .runner import Runner, read_stable_regular_file

MANAGER_INSTALLER_URL = (
    "https://raw.githubusercontent.com/dorillo/remnawave-manager/main/install.sh"
)
_MAX_INSTALLER_SIZE = 256 * 1024
_VERSION_OUTPUT = re.compile(r"^rwm [0-9]+(?:\.[0-9]+){2}(?:[A-Za-z0-9.+-]*)?$")
_INSTALLER_MARKERS = (
    "readonly DEFAULT_MANAGER_REPOSITORY='dorillo/remnawave-manager'",
    "readonly DEFAULT_MANAGER_REF='main'",
    "bootstrap_manager() {",
)


def _validate_installer(path: Path) -> None:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_INSTALLER_SIZE,
        label="Установщик Remnawave Manager",
    )
    try:
        text = snapshot.data.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ValidationError("Загруженный install.sh не является UTF-8 текстом.") from error
    if (
        not text.startswith("#!/bin/bash\n")
        or "\x00" in text
        or any(marker not in text for marker in _INSTALLER_MARKERS)
    ):
        raise ValidationError(
            "Загруженный install.sh не похож на штатный установщик Remnawave Manager."
        )


def update_manager(runner: Runner) -> str:
    temporary_root = "/tmp" if os.name == "posix" else None
    with tempfile.TemporaryDirectory(
        prefix="rwm-manager-update-", dir=temporary_root
    ) as temporary:
        installer = Path(temporary) / "install.sh"
        runner.run_to_file(
            [
                "/usr/bin/curl",
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                "300",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--tlsv1.2",
                MANAGER_INSTALLER_URL,
            ],
            installer,
            timeout=360,
        )
        _validate_installer(installer)
        runner.interactive(["/bin/bash", str(installer), "install"])

    result = runner.run(
        ["/usr/local/bin/rwm", "--version"], check=False, timeout=30
    )
    installed_version = result.stdout.strip()
    if result.returncode != 0 or _VERSION_OUTPUT.fullmatch(installed_version) is None:
        raise TransactionError(
            "Установщик завершился, но новый entrypoint не вернул версию. "
            "Проверьте: sudo /usr/local/bin/rwm --version"
        )
    return installed_version


__all__ = ["MANAGER_INSTALLER_URL", "update_manager"]
