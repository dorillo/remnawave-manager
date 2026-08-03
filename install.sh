#!/bin/bash
set -Eeuo pipefail
set +x
IFS=$'\n\t'
umask 022
export PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1
unset BASH_ENV CDPATH ENV GLOBIGNORE \
    COMPOSE_CONVERT_WINDOWS_PATHS COMPOSE_ENV_FILES COMPOSE_FILE \
    COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
    DOCKER_API_VERSION DOCKER_CERT_PATH DOCKER_CLI_PLUGIN_EXTRA_DIRS \
    DOCKER_CONFIG DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS_VERIFY \
    PIP_CONFIG_FILE PIP_EXTRA_INDEX_URL PIP_INDEX_URL PIP_REQUIRE_VIRTUALENV \
    PIP_TRUSTED_HOST PYTHONBREAKPOINT PYTHONHOME PYTHONINSPECT PYTHONPATH \
    PYTHONPYCACHEPREFIX PYTHONSTARTUP PYTHONWARNINGS VIRTUAL_ENV \
    __PYVENV_LAUNCHER__ 2>/dev/null || true

readonly DEFAULT_MANAGER_REPOSITORY='dorillo/remnawave-manager'
readonly DEFAULT_MANAGER_REF='main'

die() {
    printf '%s\n' "$1" >&2
    exit 1
}

usage() {
    printf 'Использование: %s [install]\n' "${0##*/}"
}

if (( $# > 1 )) || [[ "${1:-install}" != 'install' ]]; then
    usage >&2
    exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
    printf '%s\n' 'Установщик нужно запускать от root: sudo ./install.sh' >&2
    exit 1
fi

bootstrap_manager() {
    local temporary_directory
    local archive
    local status=0

    temporary_directory="$(mktemp -d --tmpdir rwm-bootstrap.XXXXXXXX)" \
        || die 'Не удалось создать временный каталог для загрузки Remnawave Manager.'
    archive="${temporary_directory}/remnawave-manager.tar.gz"
    trap 'rm -rf -- "${temporary_directory}"' EXIT

    printf 'Загрузка Remnawave Manager из %s (%s)...\n' \
        "${DEFAULT_MANAGER_REPOSITORY}" "${DEFAULT_MANAGER_REF}"
    if ! curl --disable --fail --silent --show-error --location --retry 3 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        "https://api.github.com/repos/${DEFAULT_MANAGER_REPOSITORY}/tarball/${DEFAULT_MANAGER_REF}" \
        --output "${archive}"; then
        die 'Не удалось скачать Remnawave Manager с GitHub.'
    fi

    mkdir -- "${temporary_directory}/source"
    if ! tar --extract --gzip --file "${archive}" \
        --directory "${temporary_directory}/source" --strip-components=1 \
        --no-same-owner --no-same-permissions; then
        die 'Не удалось распаковать архив Remnawave Manager.'
    fi
    if [[ ! -f "${temporary_directory}/source/install.sh" \
        || ! -f "${temporary_directory}/source/pyproject.toml" \
        || ! -f "${temporary_directory}/source/src/remnawave_manager/__init__.py" ]]; then
        die 'Загруженный архив Remnawave Manager неполон.'
    fi

    bash "${temporary_directory}/source/install.sh" "$@" || status=$?
    rm -rf -- "${temporary_directory}"
    trap - EXIT
    exit "${status}"
}

SCRIPT_DIR=''
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
fi
if [[ -z "${SCRIPT_DIR}" \
    || ! -f "${SCRIPT_DIR}/pyproject.toml" \
    || ! -f "${SCRIPT_DIR}/src/remnawave_manager/__init__.py" ]]; then
    command -v curl >/dev/null 2>&1 \
        || die 'Для загрузки Remnawave Manager требуется curl.'
    command -v tar >/dev/null 2>&1 \
        || die 'Для загрузки Remnawave Manager требуется tar.'
    bootstrap_manager "$@"
fi
readonly SCRIPT_DIR
readonly MANAGED_ROOT="/opt/remnawave-manager"
readonly OWNERSHIP_MARKER="${MANAGED_ROOT}/.managed-by-remnawave-manager"
readonly CONFIG_ROOT="/etc/remnawave-manager"
readonly CONFIG_OWNERSHIP_MARKER="${CONFIG_ROOT}/.managed-by-remnawave-manager"
readonly RUNTIME_DIR="${MANAGED_ROOT}/runtime"
readonly LEGACY_VENV_DIR="${RUNTIME_DIR}/venv"
readonly ACTIVE_VENV_LINK="${RUNTIME_DIR}/active"
readonly PREVIOUS_VENV_LINK="${RUNTIME_DIR}/previous"
readonly ENTRYPOINT="/usr/local/bin/rwm"
readonly MANAGER_LOCK_DIR="/run/remnawave-manager"
readonly MANAGER_LOCK="${MANAGER_LOCK_DIR}/manager.lock"
readonly MARKER_TEXT='Этот каталог управляется Remnawave Manager.'
readonly VENV_MARKER='.managed-by-remnawave-manager'
readonly GCORE_PLUGIN_VERSION='0.1.8'
readonly GCORE_PLUGIN_SHA256='2302e05aee307732f94319081e74a4f17ee2765383a2ecae3ff15fdea8e579f0'
readonly GCORE_PLUGIN_URL='https://files.pythonhosted.org/packages/5e/89/a0b459ee378254fcba11831623e26e6c73f5eb88c65e6204a924bc55b8e6/certbot_dns_gcore-0.1.8-py3-none-any.whl'

install_gcore_certbot_plugin() {
    local wheel_path
    wheel_path="$(mktemp --tmpdir rwm-certbot-dns-gcore.XXXXXXXX.whl)" \
        || die 'Не удалось создать временный файл для Certbot DNS-плагина Gcore.'

    if ! curl --disable --fail --silent --show-error --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        "${GCORE_PLUGIN_URL}" --output "${wheel_path}"; then
        rm -f -- "${wheel_path}"
        die "Не удалось скачать закреплённый certbot-dns-gcore ${GCORE_PLUGIN_VERSION} с PyPI."
    fi
    if ! printf '%s  %s\n' "${GCORE_PLUGIN_SHA256}" "${wheel_path}" \
        | sha256sum --check --status; then
        rm -f -- "${wheel_path}"
        die 'SHA-256 пакета certbot-dns-gcore не совпал; установка остановлена.'
    fi
    if ! python3 -m pip --isolated install \
        --break-system-packages \
        --disable-pip-version-check \
        --no-deps \
        "${wheel_path}"; then
        rm -f -- "${wheel_path}"
        die 'Не удалось установить проверенный Certbot DNS-плагин Gcore в системный Python.'
    fi
    rm -f -- "${wheel_path}"

    if ! certbot plugins 2>/dev/null \
        | grep -Eq '^[[:space:]]*\*?[[:space:]]*dns-gcore[[:space:]]*$'; then
        die 'Certbot не обнаружил установленный DNS-плагин Gcore; автопродление было бы неработоспособно.'
    fi
}

enable_ubuntu_universe() {
    if ! apt-get update; then
        die 'Не удалось обновить индексы APT. Проверьте доступность зеркал Ubuntu 24.04 (Noble).'
    fi

    if ! command -v add-apt-repository >/dev/null 2>&1; then
        if ! apt-get install -y --no-install-recommends software-properties-common; then
            die 'Не удалось установить software-properties-common из основного репозитория Ubuntu.'
        fi
    fi

    if ! add-apt-repository --yes --no-update universe; then
        die 'Не удалось включить официальный компонент APT Universe для Ubuntu 24.04.'
    fi
    if ! apt-get update; then
        die 'Компонент APT Universe включён, но обновить индексы пакетов не удалось.'
    fi
}

dpkg_package_is_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null \
        | grep -qx 'install ok installed'
}

install_docker_components() {
    if ! command -v docker >/dev/null 2>&1; then
        if ! apt-get install -y --no-install-recommends docker.io docker-compose-v2; then
            die 'Не удалось установить docker.io и docker-compose-v2 из Ubuntu Universe.'
        fi
    elif ! docker compose version >/dev/null 2>&1; then
        if dpkg_package_is_installed docker-ce; then
            if ! apt-get install -y --no-install-recommends docker-compose-plugin; then
                die 'Docker CE найден, но docker-compose-plugin установить не удалось. Проверьте официальный APT-репозиторий Docker.'
            fi
        elif dpkg_package_is_installed docker.io; then
            if ! apt-get install -y --no-install-recommends docker-compose-v2; then
                die 'Docker из Ubuntu найден, но docker-compose-v2 установить не удалось. Проверьте компонент APT Universe.'
            fi
        else
            die 'Обнаружена команда docker без Compose, но она не принадлежит пакетам docker.io или docker-ce. Установщик не изменяет неизвестную установку Docker.'
        fi
    fi

    if ! docker compose version >/dev/null 2>&1; then
        die 'Docker Compose v2 недоступен: команда «docker compose version» завершилась ошибкой.'
    fi
}

ensure_rootful_docker_service() {
    local docker_load_state
    local docker_security_options

    if ! command -v systemctl >/dev/null 2>&1; then
        die 'Не найден systemctl. Требуется стандартный системный rootful Docker с unit docker.service.'
    fi
    if ! docker_load_state="$(systemctl show --property=LoadState --value docker.service 2>/dev/null)"; then
        die 'Не удалось обратиться к systemd. Требуется Ubuntu 24.04 с запущенным systemd и системным docker.service.'
    fi
    if [[ "${docker_load_state}" != "loaded" ]]; then
        die "docker.service недоступен (LoadState=${docker_load_state:-неизвестно}). Rootless Docker, Docker через snap и удалённый DOCKER_HOST не поддерживаются; установите системный rootful Docker."
    fi

    if ! systemctl enable --now docker.service; then
        printf '%s\n' 'Не удалось включить и запустить docker.service. Текущее состояние:' >&2
        systemctl --no-pager --full status docker.service >&2 || true
        die 'Исправьте ошибку docker.service и повторно запустите установщик.'
    fi
    if ! systemctl is-active --quiet docker.service; then
        systemctl --no-pager --full status docker.service >&2 || true
        die 'docker.service установлен, но не перешёл в активное состояние.'
    fi
    if [[ ! -S /run/docker.sock ]]; then
        die 'docker.service активен, но системный сокет /run/docker.sock не создан. Пользовательский или rootless Docker не поддерживается.'
    fi
    if ! docker_security_options="$(docker --host unix:///run/docker.sock info --format '{{json .SecurityOptions}}' 2>/dev/null)"; then
        die 'Не удалось подключиться к rootful Docker через /run/docker.sock. Проверьте docker.service и его журнал.'
    fi
    if [[ "${docker_security_options}" == *'name=rootless'* ]]; then
        die 'Docker на /run/docker.sock работает в rootless-режиме. Требуется стандартный системный rootful docker.service.'
    fi
}

validate_managed_directory() {
    local path="$1"
    local metadata
    local permissions

    if [[ -L "${path}" || ( -e "${path}" && ! -d "${path}" ) ]]; then
        die "Управляемый путь ${path} имеет небезопасный тип."
    fi
    if [[ ! -e "${path}" ]]; then
        return
    fi
    if ! metadata="$(stat -c '%u:%g:%a' -- "${path}")"; then
        die "Не удалось проверить владельца и права управляемого каталога ${path}."
    fi
    if [[ "${metadata%:*}" != '0:0' ]]; then
        die "Управляемый каталог ${path} должен принадлежать root:root."
    fi
    permissions="${metadata##*:}"
    if (( (8#${permissions} & 0022) != 0 )); then
        die "Управляемый каталог ${path} доступен для записи группе или другим пользователям."
    fi
}

validate_ownership_marker() {
    local path="$1"
    local expected_mode="$2"
    local required="${3:-false}"
    local metadata
    local marker_size

    if [[ ! -e "${path}" && ! -L "${path}" ]]; then
        if [[ "${required}" == 'true' ]]; then
            die "Обязательный ownership-маркер ${path} отсутствует."
        fi
        return
    fi
    if [[ -L "${path}" || ! -f "${path}" ]]; then
        die "Маркер ${path} имеет небезопасный тип."
    fi
    if ! metadata="$(stat -c '%u:%g:%h:%a:%s' -- "${path}")" \
        || [[ "${metadata%:*}" != "0:0:1:${expected_mode}" ]]; then
        die "Маркер ${path} должен быть обычным single-link файлом root:root с правами ${expected_mode}."
    fi
    marker_size="${metadata##*:}"
    if (( marker_size > 1024 )); then
        die "Маркер ${path} превышает допустимый размер."
    fi
    if [[ "$(<"${path}")" != "${MARKER_TEXT}" ]]; then
        die "Маркер ${path} повреждён или не принадлежит Remnawave Manager."
    fi
}

managed_link_state() {
    local path="$1"
    local target

    if [[ -L "${path}" ]]; then
        if ! target="$(readlink -- "${path}")"; then
            die "Не удалось прочитать управляемую ссылку ${path}."
        fi
        printf 'link:%s\n' "${target}"
    elif [[ -e "${path}" ]]; then
        printf 'other\n'
    else
        printf 'absent\n'
    fi
}

assert_managed_link_unchanged() {
    local path="$1"
    local expected_state="$2"
    local current_state

    current_state="$(managed_link_state "${path}")"
    if [[ "${current_state}" != "${expected_state}" ]]; then
        die "Путь ${path} был изменён параллельно; установка остановлена без переключения версии."
    fi
}

read_os_release_value() {
    local path="$1"
    local key="$2"
    local line
    local value=''
    local found='false'

    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" != "${key}="* ]]; then
            continue
        fi
        if [[ "${found}" == 'true' ]]; then
            printf 'Параметр %s повторяется в %s.\n' "${key}" "${path}" >&2
            return 1
        fi
        found='true'
        value="${line#*=}"
    done < "${path}"
    if [[ "${found}" != 'true' ]]; then
        printf 'Параметр %s отсутствует в %s.\n' "${key}" "${path}" >&2
        return 1
    fi
    if [[ "${value}" == \"*\" && "${#value}" -ge 2 ]]; then
        value="${value:1:${#value}-2}"
    fi
    if [[ ! "${value}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        printf 'Параметр %s в %s имеет небезопасный формат.\n' "${key}" "${path}" >&2
        return 1
    fi
    printf '%s\n' "${value}"
}

if ! command -v flock >/dev/null 2>&1; then
    die 'Не найден flock из util-linux; невозможно безопасно заблокировать параллельный запуск.'
fi
if [[ -L "${MANAGER_LOCK_DIR}" || ( -e "${MANAGER_LOCK_DIR}" && ! -d "${MANAGER_LOCK_DIR}" ) ]]; then
    die "Lock-каталог ${MANAGER_LOCK_DIR} имеет небезопасный тип."
fi
if ! install -d -o root -g root -m 0700 -- "${MANAGER_LOCK_DIR}"; then
    die "Не удалось создать защищённый lock-каталог ${MANAGER_LOCK_DIR}."
fi
if [[ "$(stat -c '%u:%g:%a' -- "${MANAGER_LOCK_DIR}")" != '0:0:700' ]]; then
    die "Lock-каталог ${MANAGER_LOCK_DIR} должен принадлежать root:root и иметь права 0700."
fi
if [[ -L "${MANAGER_LOCK}" ]]; then
    die "Lock-файл ${MANAGER_LOCK} является символической ссылкой."
fi
if [[ -e "${MANAGER_LOCK}" && ( ! -f "${MANAGER_LOCK}" || "$(stat -c '%u:%g:%h' -- "${MANAGER_LOCK}")" != '0:0:1' ) ]]; then
    die "Lock-файл ${MANAGER_LOCK} имеет небезопасный тип, ownership или hardlink."
fi
if ! exec 9> "${MANAGER_LOCK}"; then
    die "Не удалось безопасно открыть lock-файл ${MANAGER_LOCK}."
fi
if ! chmod 0600 "${MANAGER_LOCK}"; then
    die "Не удалось ограничить права lock-файла ${MANAGER_LOCK}."
fi
if ! flock --nonblock 9; then
    die 'Другая операция Remnawave Manager или install.sh уже выполняется.'
fi

if ! KERNEL_NAME="$(uname -s 2>/dev/null)"; then
    die 'Не удалось определить тип ядра. Поддерживается только Linux.'
fi
readonly KERNEL_NAME
if [[ "${KERNEL_NAME}" != "Linux" ]]; then
    die "Неподдерживаемая платформа ${KERNEL_NAME}. Поддерживается только Linux."
fi

OS_RELEASE_PATH='/etc/os-release'
if [[ -L "${OS_RELEASE_PATH}" ]]; then
    if ! OS_RELEASE_PATH="$(readlink -f -- "${OS_RELEASE_PATH}")" \
        || [[ "${OS_RELEASE_PATH}" != '/usr/lib/os-release' ]]; then
        die 'Ссылка /etc/os-release должна указывать на штатный /usr/lib/os-release.'
    fi
fi
if [[ ! -f "${OS_RELEASE_PATH}" || -L "${OS_RELEASE_PATH}" ]]; then
    die 'Не удалось безопасно определить операционную систему: os-release не является обычным файлом.'
fi
if ! os_release_metadata="$(stat -c '%u:%g:%h:%a:%s' -- "${OS_RELEASE_PATH}")"; then
    die 'Не удалось проверить metadata os-release.'
fi
os_release_size="${os_release_metadata##*:}"
os_release_prefix="${os_release_metadata%:*}"
os_release_mode="${os_release_prefix##*:}"
os_release_identity="${os_release_prefix%:*}"
if [[ "${os_release_identity}" != '0:0:1' ]] \
    || (( (8#${os_release_mode} & 0022) != 0 )) \
    || (( os_release_size > 65536 )); then
    die 'os-release должен быть single-link файлом root:root, недоступным для записи группе/остальным и не больше 64 KiB.'
fi
if ! OS_ID="$(read_os_release_value "${OS_RELEASE_PATH}" 'ID')" \
    || ! OS_VERSION_ID="$(read_os_release_value "${OS_RELEASE_PATH}" 'VERSION_ID')"; then
    die 'Не удалось безопасно прочитать ID и VERSION_ID из os-release.'
fi
readonly OS_RELEASE_PATH OS_ID OS_VERSION_ID
if [[ "${OS_ID}" != "ubuntu" || "${OS_VERSION_ID}" != "24.04" ]]; then
    printf '%s\n' 'Поддерживается только Ubuntu 24.04 LTS.' >&2
    exit 1
fi

if ! command -v dpkg >/dev/null 2>&1; then
    die 'Не найден dpkg; невозможно проверить архитектуру Ubuntu.'
fi
if ! HOST_ARCHITECTURE="$(dpkg --print-architecture 2>/dev/null)"; then
    die 'Команда «dpkg --print-architecture» завершилась ошибкой.'
fi
readonly HOST_ARCHITECTURE
case "${HOST_ARCHITECTURE}" in
    amd64 | arm64) ;;
    *) die "Архитектура ${HOST_ARCHITECTURE} не поддерживается. Допустимы только amd64 и arm64." ;;
esac

validate_managed_directory "${MANAGED_ROOT}"
validate_managed_directory "${RUNTIME_DIR}"
validate_managed_directory "${CONFIG_ROOT}"

if [[ -e "${LEGACY_VENV_DIR}" || -L "${LEGACY_VENV_DIR}" ]] \
    && { [[ -L "${LEGACY_VENV_DIR}" ]] || [[ ! -d "${LEGACY_VENV_DIR}" ]]; }; then
    die "Legacy venv ${LEGACY_VENV_DIR} имеет неожиданный тип; установка остановлена."
fi

if [[ -e "${ACTIVE_VENV_LINK}" || -L "${ACTIVE_VENV_LINK}" ]]; then
    if [[ ! -L "${ACTIVE_VENV_LINK}" ]] \
        || ! active_venv_target="$(readlink -f -- "${ACTIVE_VENV_LINK}")" \
        || [[ "${active_venv_target}" != "${RUNTIME_DIR}"/venv.release.* ]] \
        || [[ ! -d "${active_venv_target}" ]] \
        || [[ -L "${active_venv_target}" ]]; then
        die "Ссылка ${ACTIVE_VENV_LINK} не указывает на принадлежащее менеджеру окружение."
    fi
    validate_managed_directory "${active_venv_target}"
    validate_ownership_marker "${active_venv_target}/${VENV_MARKER}" '644' true
fi
if [[ -e "${PREVIOUS_VENV_LINK}" || -L "${PREVIOUS_VENV_LINK}" ]]; then
    if [[ ! -L "${PREVIOUS_VENV_LINK}" ]] \
        || ! previous_venv_target="$(readlink -f -- "${PREVIOUS_VENV_LINK}")" \
        || { [[ "${previous_venv_target}" != "${RUNTIME_DIR}"/venv.release.* ]] \
            && [[ "${previous_venv_target}" != "${LEGACY_VENV_DIR}" ]]; } \
        || [[ ! -d "${previous_venv_target}" ]] \
        || [[ -L "${previous_venv_target}" ]]; then
        die "Ссылка ${PREVIOUS_VENV_LINK} не указывает на принадлежащее менеджеру окружение."
    fi
    validate_managed_directory "${previous_venv_target}"
    validate_ownership_marker "${previous_venv_target}/${VENV_MARKER}" '644' true
fi

validate_ownership_marker "${OWNERSHIP_MARKER}" '644'
validate_ownership_marker "${CONFIG_OWNERSHIP_MARKER}" '600'

if [[ -d "${MANAGED_ROOT}" && ! -e "${OWNERSHIP_MARKER}" ]] \
    && find "${MANAGED_ROOT}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    printf '%s\n' "Каталог ${MANAGED_ROOT} не принадлежит Remnawave Manager; установка остановлена." >&2
    exit 1
fi
if [[ -d "${CONFIG_ROOT}" && ! -e "${CONFIG_OWNERSHIP_MARKER}" ]] \
    && find "${CONFIG_ROOT}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    printf '%s\n' "Каталог ${CONFIG_ROOT} не принадлежит Remnawave Manager; установка остановлена." >&2
    exit 1
fi

if [[ -e "${ENTRYPOINT}" || -L "${ENTRYPOINT}" ]]; then
    if [[ ! -L "${ENTRYPOINT}" ]] \
        || { [[ "$(readlink -- "${ENTRYPOINT}")" != "${LEGACY_VENV_DIR}/bin/rwm" ]] \
            && [[ "$(readlink -- "${ENTRYPOINT}")" != "${ACTIVE_VENV_LINK}/bin/rwm" ]]; }; then
        printf '%s\n' "Путь ${ENTRYPOINT} уже занят другим файлом; установка остановлена." >&2
        exit 1
    fi
fi

initial_active_link_state="$(managed_link_state "${ACTIVE_VENV_LINK}")"
readonly initial_active_link_state
initial_previous_link_state="$(managed_link_state "${PREVIOUS_VENV_LINK}")"
readonly initial_previous_link_state
initial_entrypoint_state="$(managed_link_state "${ENTRYPOINT}")"
readonly initial_entrypoint_state

export DEBIAN_FRONTEND=noninteractive
enable_ubuntu_universe
if ! apt-get install -y --no-install-recommends \
    ca-certificates \
    certbot \
    curl \
    iproute2 \
    kmod \
    openssh-server \
    openssl \
    procps \
    python3 \
    python3-certbot-dns-cloudflare \
    python3-pip \
    python3-setuptools \
    python3-venv \
    unattended-upgrades \
    ufw; then
    die 'Не удалось установить системные зависимости. Проверьте доступность компонентов APT Main и Universe для Ubuntu 24.04.'
fi

install_gcore_certbot_plugin

install_docker_components
ensure_rootful_docker_service
install -d -o root -g root -m 0755 "${MANAGED_ROOT}" "${RUNTIME_DIR}"
install -d -o root -g root -m 0700 "${CONFIG_ROOT}"
printf '%s\n' "${MARKER_TEXT}" > "${OWNERSHIP_MARKER}"
printf '%s\n' "${MARKER_TEXT}" > "${CONFIG_OWNERSHIP_MARKER}"
chmod 0644 "${OWNERSHIP_MARKER}"
chmod 0600 "${CONFIG_OWNERSHIP_MARKER}"

if ! staged_venv="$(mktemp -d --tmpdir="${RUNTIME_DIR}" 'venv.release.XXXXXXXX')"; then
    die 'Не удалось создать staging venv.'
fi
release_switch_started='false'
release_switch_committed='false'
cleanup_install_artifacts() {
    local exit_status="$?"
    local cleanup_failed='false'
    local rollback_incomplete='false'

    trap - EXIT
    trap '' HUP INT TERM
    if [[ "${release_switch_started:-false}" == 'true' \
        && "${release_switch_committed:-false}" != 'true' ]]; then
        if ! declare -F restore_release_links >/dev/null \
            || ! restore_release_links; then
            rollback_incomplete='true'
            cleanup_failed='true'
            printf '%s\n' \
                'Автоматический возврат release-ссылок выполнен не полностью; staging окружение сохранено для ручного восстановления.' \
                >&2
        else
            release_switch_started='false'
        fi
    fi
    if [[ -n "${staged_venv:-}" && -d "${staged_venv}" && ! -L "${staged_venv}" ]] \
        && [[ "${staged_venv}" == "${RUNTIME_DIR}"/venv.release.* ]] \
        && [[ "${rollback_incomplete}" == 'false' ]] \
        && { [[ ! -L "${ACTIVE_VENV_LINK}" ]] \
            || [[ "$(readlink -f -- "${ACTIVE_VENV_LINK}" 2>/dev/null || true)" != "${staged_venv}" ]]; }; then
        if ! rm -rf -- "${staged_venv}"; then
            cleanup_failed='true'
            printf 'Не удалось удалить staging окружение %s.\n' "${staged_venv}" >&2
        fi
    fi
    if ! rm -f -- "${ACTIVE_VENV_LINK}.new.$$" "${PREVIOUS_VENV_LINK}.new.$$" \
        "${ENTRYPOINT}.new.$$"; then
        cleanup_failed='true'
        printf '%s\n' 'Не удалось удалить временные release-ссылки установщика.' >&2
    fi
    if [[ "${cleanup_failed}" == 'true' && "${exit_status}" -eq 0 ]]; then
        exit_status=1
    fi
    exit "${exit_status}"
}
trap cleanup_install_artifacts EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! python3 -m venv --system-site-packages "${staged_venv}"; then
    die 'Не удалось создать изолированное Python-окружение менеджера.'
fi
chmod 0755 "${staged_venv}"

if ! "${staged_venv}/bin/python" -m pip --isolated install \
    --disable-pip-version-check \
    --upgrade \
    --no-build-isolation \
    --no-deps \
    "${SCRIPT_DIR}"; then
    die 'Не удалось установить Remnawave Manager в staging venv; активная версия не изменена.'
fi
printf '%s\n' "${MARKER_TEXT}" > "${staged_venv}/${VENV_MARKER}"
chmod 0644 "${staged_venv}/${VENV_MARKER}"
if ! "${staged_venv}/bin/python" -c \
    'import remnawave_manager; from remnawave_manager.cli import build_parser; build_parser()' \
    || ! "${staged_venv}/bin/rwm" --help >/dev/null; then
    die 'Staging venv не прошёл проверку импорта и entrypoint; активная версия не изменена.'
fi

assert_managed_link_unchanged "${ACTIVE_VENV_LINK}" "${initial_active_link_state}"
assert_managed_link_unchanged "${PREVIOUS_VENV_LINK}" "${initial_previous_link_state}"
assert_managed_link_unchanged "${ENTRYPOINT}" "${initial_entrypoint_state}"
validate_managed_directory "${MANAGED_ROOT}"
validate_managed_directory "${RUNTIME_DIR}"
validate_managed_directory "${CONFIG_ROOT}"
validate_ownership_marker "${OWNERSHIP_MARKER}" '644' true
validate_ownership_marker "${CONFIG_OWNERSHIP_MARKER}" '600' true

previous_active_target=''
if [[ -L "${ACTIVE_VENV_LINK}" ]]; then
    if ! previous_active_target="$(readlink -f -- "${ACTIVE_VENV_LINK}")"; then
        die "Не удалось повторно разрешить ссылку ${ACTIVE_VENV_LINK}."
    fi
fi
old_previous_target=''
if [[ -L "${PREVIOUS_VENV_LINK}" ]]; then
    if ! old_previous_target="$(readlink -f -- "${PREVIOUS_VENV_LINK}")"; then
        die "Не удалось повторно разрешить ссылку ${PREVIOUS_VENV_LINK}."
    fi
    validate_managed_directory "${old_previous_target}"
    validate_ownership_marker "${old_previous_target}/${VENV_MARKER}" '644' true
fi
rollback_target="${previous_active_target:-${LEGACY_VENV_DIR}}"
if [[ ! -d "${rollback_target}" || -L "${rollback_target}" ]]; then
    rollback_target="${old_previous_target}"
fi

restore_managed_link() {
    local path="$1"
    local previous_state="$2"
    local transaction_state="$3"
    local current_state
    local previous_target

    current_state="$(managed_link_state "${path}")"
    if [[ "${current_state}" == "${previous_state}" ]]; then
        return 0
    fi
    if [[ "${current_state}" != "${transaction_state}" ]]; then
        printf 'Путь %s был изменён вне транзакции; автоматический rollback его не перезаписывает.\n' \
            "${path}" >&2
        return 1
    fi

    case "${previous_state}" in
        absent)
            rm -f -- "${path}"
            ;;
        link:*)
            previous_target="${previous_state#link:}"
            rm -f -- "${path}.new.$$"
            ln -s -- "${previous_target}" "${path}.new.$$" \
                && mv -Tf -- "${path}.new.$$" "${path}"
            ;;
        *)
            printf 'Исходное состояние release-ссылки %s некорректно; путь сохранён.\n' \
                "${path}" >&2
            return 1
            ;;
    esac
}

restore_release_links() {
    local rollback_failed='false'
    local transaction_previous_state="${initial_previous_link_state}"

    rm -f -- "${ACTIVE_VENV_LINK}.new.$$" "${PREVIOUS_VENV_LINK}.new.$$" \
        "${ENTRYPOINT}.new.$$" || rollback_failed='true'
    if [[ -n "${rollback_target}" ]]; then
        transaction_previous_state="link:${rollback_target}"
    fi
    if ! restore_managed_link \
        "${ACTIVE_VENV_LINK}" \
        "${initial_active_link_state}" \
        "link:${staged_venv}"; then
        rollback_failed='true'
    fi
    if ! restore_managed_link \
        "${ENTRYPOINT}" \
        "${initial_entrypoint_state}" \
        "link:${ACTIVE_VENV_LINK}/bin/rwm"; then
        rollback_failed='true'
    fi
    if ! restore_managed_link \
        "${PREVIOUS_VENV_LINK}" \
        "${initial_previous_link_state}" \
        "${transaction_previous_state}"; then
        rollback_failed='true'
    fi
    [[ "${rollback_failed}" == 'false' ]]
}

release_switch_started='true'
if [[ -n "${rollback_target}" ]]; then
    validate_managed_directory "${rollback_target}"
    if [[ "${rollback_target}" == "${LEGACY_VENV_DIR}" ]]; then
        validate_ownership_marker "${rollback_target}/${VENV_MARKER}" '644'
        if [[ ! -e "${rollback_target}/${VENV_MARKER}" ]]; then
            printf '%s\n' "${MARKER_TEXT}" > "${rollback_target}/${VENV_MARKER}"
            chmod 0644 "${rollback_target}/${VENV_MARKER}"
        fi
    else
        validate_ownership_marker "${rollback_target}/${VENV_MARKER}" '644' true
    fi
    if ! ln -s -- "${rollback_target}" "${PREVIOUS_VENV_LINK}.new.$$" \
        || ! mv -Tf -- "${PREVIOUS_VENV_LINK}.new.$$" "${PREVIOUS_VENV_LINK}"; then
        if restore_release_links; then
            release_switch_started='false'
            die 'Не удалось подготовить rollback pointer; исходные release-ссылки восстановлены.'
        fi
        die 'Не удалось подготовить rollback pointer, а исходные release-ссылки восстановлены не полностью.'
    fi
fi

if ! ln -s -- "${staged_venv}" "${ACTIVE_VENV_LINK}.new.$$" \
    || ! mv -Tf -- "${ACTIVE_VENV_LINK}.new.$$" "${ACTIVE_VENV_LINK}" \
    || ! ln -s -- "${ACTIVE_VENV_LINK}/bin/rwm" "${ENTRYPOINT}.new.$$" \
    || ! mv -Tf -- "${ENTRYPOINT}.new.$$" "${ENTRYPOINT}"; then
    if restore_release_links; then
        release_switch_started='false'
        die 'Не удалось завершить переключение менеджера; предыдущая версия восстановлена.'
    fi
    die 'Не удалось завершить переключение менеджера, а автоматический возврат ссылок выполнен не полностью; используйте rollback pointer и не удаляйте окружения runtime.'
fi

if ! "${ENTRYPOINT}" --help >/dev/null; then
    if ! restore_release_links; then
        die 'Новый entrypoint не запускается, а автоматический возврат ссылок завершён не полностью; используйте rollback pointer и не удаляйте окружения runtime.'
    fi
    release_switch_started='false'
    die 'Новый entrypoint не запускается; предыдущая версия менеджера восстановлена.'
fi

release_switch_committed='true'
staged_venv=''
if [[ -n "${old_previous_target}" && "${old_previous_target}" != "${rollback_target}" ]] \
    && { [[ "${old_previous_target}" == "${RUNTIME_DIR}"/venv.release.* ]] \
        || [[ "${old_previous_target}" == "${LEGACY_VENV_DIR}" ]]; } \
    && [[ -f "${old_previous_target}/${VENV_MARKER}" ]] \
    && [[ "$(<"${old_previous_target}/${VENV_MARKER}")" == "${MARKER_TEXT}" ]] \
    && [[ -d "${old_previous_target}" && ! -L "${old_previous_target}" ]]; then
    if ! rm -rf -- "${old_previous_target}"; then
        printf 'Новая версия активирована, но устаревшее окружение %s удалить не удалось.\n' \
            "${old_previous_target}" >&2
    fi
fi
trap - EXIT HUP INT TERM

if ! "${ENTRYPOINT}" system apply --yes; then
    printf '%s\n' \
        'Remnawave Manager установлен, но BBR/fq или unattended-upgrades не удалось применить. Выполните позднее: sudo rwm system apply' \
        >&2
fi
printf '%s\n' 'Remnawave Manager установлен. Запустите: sudo rwm'
