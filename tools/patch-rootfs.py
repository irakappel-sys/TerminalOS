#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path


DEBIAN_SOURCES = """deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb https://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware
deb https://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
"""


FINALIZER = """#!/bin/sh
set -eu

umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LOG=/var/log/terminalos-installer-finalize.log
mkdir -p /var/log
exec >>"$LOG" 2>&1

echo "===== $(date --iso-8601=seconds) ====="
echo "Removing live-installer packages and artifacts."

export DEBIAN_FRONTEND=noninteractive
packages=""
for package in \
    terminalos-installer-config \
    calamares-settings-debian \
    calamares \
    live-tools \
    live-boot-doc \
    live-boot-initramfs-tools \
    live-boot
do
    if /usr/bin/dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | /usr/bin/grep -qx installed; then
        packages="$packages $package"
    fi
done

if [ -n "$packages" ]; then
    # Package removal restores diversions and records the target state correctly.
    # The currently-running script remains available through the shell's open fd.
    /usr/bin/apt-get purge --yes --no-install-recommends $packages
fi

rm -f \
    /usr/local/bin/terminalos-installer \
    /usr/local/bin/terminalos-installer-autostart \
    /usr/local/libexec/terminalos-calamares-root \
    /usr/local/libexec/terminalos-install-mode \
    /usr/share/applications/terminalos-installer.desktop \
    /usr/share/applications/calamares.desktop \
    /usr/share/applications/calamares.desktop.orig \
    /usr/share/applications/install-debian.desktop \
    /usr/bin/calamares-install-debian \
    /usr/share/applications/calamares-install-debian.desktop \
    /usr/share/pixmaps/install-debian.png \
    /etc/xdg/autostart/terminalos-installer-autostart.desktop \
    /etc/xdg/autostart/calamares-desktop-icon.desktop \
    /etc/systemd/system/gdm.service.d/80-terminalos-install-mode.conf \
    /etc/systemd/system/terminalos-install-mode.service \
    /etc/systemd/system/graphical.target.wants/terminalos-install-mode.service \
    /etc/sudoers.d/terminalos-installer \
    /etc/security/pwquality.conf.d/99-terminalos-permissive.conf

rm -rf \
    /etc/calamares \
    /root/.cache/calamares

for required in \
    /usr/bin/apt-get \
    /usr/bin/dpkg \
    /usr/bin/tos \
    /usr/lib/terminalos/tos-backend \
    /etc/apt/sources.list \
    /var/lib/dpkg/status
do
    if [ ! -e "$required" ]; then
        echo "Required installed-system component is missing: $required" >&2
        exit 1
    fi
done

if [ -e /etc/sudoers.d/terminalos-installer ] || [ -d /etc/calamares ]; then
    echo "Live-installer access survived finalization." >&2
    exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
fi

mkdir -p /var/lib/terminalos
date --iso-8601=seconds > /var/lib/terminalos/installer-components-removed
echo "TerminalOS installer cleanup completed. APT, DPKG, repositories, and tos were preserved."
"""


LIVE_LOGIN = """#!/bin/sh
set -eu

LOG=/run/terminalos-install-mode.log
exec >>"$LOG" 2>&1

echo "===== $(date --iso-8601=seconds) ====="
echo "Kernel command line: $(cat /proc/cmdline)"

getent passwd ira >/dev/null 2>&1 || {
    echo "Live user ira does not exist."
    exit 1
}

python3 - <<'PY'
from pathlib import Path
import re

path = Path("/etc/gdm3/daemon.conf")
text = path.read_text() if path.exists() else ""

if "[daemon]" not in text:
    text = "[daemon]\\n" + text


def set_key(data: str, key: str, value: str) -> str:
    pattern = rf"(?m)^\\s*{re.escape(key)}\\s*=.*$"
    replacement = f"{key}={value}"

    if re.search(pattern, data):
        return re.sub(pattern, replacement, data, count=1)

    return data.replace(
        "[daemon]",
        f"[daemon]\\n{replacement}",
        1,
    )


text = set_key(text, "AutomaticLoginEnable", "true")
text = set_key(text, "AutomaticLogin", "ira")

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text)
PY

echo "Passwordless GDM login enabled for the locked live account."
"""


LIVE_SUDOERS = """Defaults!/usr/local/libexec/terminalos-calamares-root env_keep += "DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR XDG_SESSION_TYPE WAYLAND_DISPLAY"
ira ALL=(ALL:ALL) NOPASSWD: ALL
"""


REMOVE_LIVE_USERS = """---
dontChroot: false
timeout: 60
verbose: true

script:
    - "for user in ira terminal; do if getent passwd \\"$user\\" >/dev/null 2>&1; then userdel --force --remove \\"$user\\"; fi; done"
    - "for user in ira terminal; do if getent passwd \\"$user\\" >/dev/null 2>&1; then echo \\"Temporary account survived removal: $user\\" >&2; exit 1; fi; done"
    - "rm -f /var/lib/AccountsService/users/ira /var/lib/AccountsService/users/terminal"
"""


FINALIZER_MODULE = """---
dontChroot: false
timeout: 600
verbose: true

script:
    - "/usr/local/libexec/terminalos-finalize-installed-system"
"""


def replace_account_password(shadow_path: Path, username: str) -> None:
    if not shadow_path.exists():
        return

    lines = shadow_path.read_text().splitlines()
    matches = 0

    for index, line in enumerate(lines):
        fields = line.split(":")

        if fields[0] != username:
            continue

        if len(fields) != 9:
            raise RuntimeError(f"Malformed shadow entry in {shadow_path}")

        fields[1] = "!"
        lines[index] = ":".join(fields)
        matches += 1

    if matches != 1:
        raise RuntimeError(
            f"Expected one {username} entry in {shadow_path}, found {matches}"
        )

    shadow_path.write_text("\n".join(lines) + "\n")


def remove_supplementary_membership(database: Path, username: str) -> None:
    if not database.exists():
        return

    lines = database.read_text().splitlines()

    for index, line in enumerate(lines):
        fields = line.split(":")

        if len(fields) != 4:
            raise RuntimeError(f"Malformed account database entry in {database}")

        members = [member for member in fields[3].split(",") if member]
        fields[3] = ",".join(member for member in members if member != username)
        lines[index] = ":".join(fields)

    database.write_text("\n".join(lines) + "\n")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def scrub_build_residue(rootfs: Path) -> None:
    generated_files = (
        rootfs / "root/.bash_history",
        rootfs / "root/.lesshst",
        rootfs / "home/ira/.bash_history",
        rootfs / "home/ira/.lesshst",
        rootfs / "home/terminal/.bash_history",
        rootfs / "home/terminal/.lesshst",
        rootfs / "var/cache/apt/pkgcache.bin",
        rootfs / "var/cache/apt/srcpkgcache.bin",
    )

    for path in generated_files:
        path.unlink(missing_ok=True)

    shutil.rmtree(rootfs / "root/.cache/calamares", ignore_errors=True)


def remove_public_random_seeds(rootfs: Path) -> None:
    seeds = (
        rootfs / ".random-seed",
        rootfs / "var/lib/systemd/random-seed",
        rootfs / "var/lib/urandom/random-seed",
    )

    for path in seeds:
        path.unlink(missing_ok=True)


def reset_machine_identity(rootfs: Path) -> None:
    machine_id = rootfs / "etc/machine-id"
    mode = machine_id.stat().st_mode & 0o7777 if machine_id.exists() else 0o444
    if machine_id.exists():
        machine_id.chmod(mode | 0o200)
    machine_id.write_text("")
    machine_id.chmod(mode)

    dbus_machine_id = rootfs / "var/lib/dbus/machine-id"
    dbus_machine_id.unlink(missing_ok=True)
    dbus_machine_id.symlink_to("/etc/machine-id")


def update_package_md5sums(rootfs: Path, relative_paths: tuple[str, ...]) -> None:
    md5sums = rootfs / "var/lib/dpkg/info/terminalos-installer-config.md5sums"
    entries: dict[str, str] = {}

    for line in md5sums.read_text().splitlines():
        digest, relative_path = line.split(maxsplit=1)
        entries[relative_path] = digest

    for relative_path in relative_paths:
        if relative_path not in entries:
            raise RuntimeError(f"Package md5sums has no entry for {relative_path}")

        content = (rootfs / relative_path).read_bytes()
        entries[relative_path] = hashlib.md5(content, usedforsecurity=False).hexdigest()

    md5sums.write_text(
        "".join(f"{digest}  {path}\n" for path, digest in entries.items())
    )


def account_password(rootfs: Path, database: str, username: str) -> str | None:
    path = rootfs / "etc" / database

    if not path.exists():
        return None

    entries = [
        line.split(":")
        for line in path.read_text().splitlines()
        if line.startswith(f"{username}:")
    ]

    if len(entries) != 1:
        raise RuntimeError(f"Expected one {username} entry in {path}, found {len(entries)}")

    return entries[0][1]


def verify(rootfs: Path) -> None:
    for database in ("shadow", "shadow-"):
        for username in ("ira", "terminal"):
            if account_password(rootfs, database, username) != "!":
                raise RuntimeError(f"{username} still has password material in {database}")

    for database in ("group", "group-", "gshadow", "gshadow-"):
        path = rootfs / "etc" / database

        if not path.exists():
            continue

        for line in path.read_text().splitlines():
            fields = line.split(":")
            members = [member for member in fields[3].split(",") if member]

            if "terminal" in members:
                raise RuntimeError(f"terminal retains supplementary access in {path}")

    passwd_entry = next(
        line
        for line in (rootfs / "etc/passwd").read_text().splitlines()
        if line.startswith("terminal:")
    )
    if not passwd_entry.endswith(":/usr/sbin/nologin"):
        raise RuntimeError("The dormant terminal account is not restricted to nologin")

    finalizer = rootfs / "usr/local/libexec/terminalos-finalize-installed-system"
    finalizer_text = finalizer.read_text()
    forbidden = (
        "rm -rf /var/lib/dpkg",
        "rm -rf /var/lib/apt",
        "rm -rf /etc/apt",
        "rm -f /usr/bin/apt-get",
        "rm -f /usr/bin/dpkg",
        "rm -rf /usr/lib/terminalos",
    )

    for value in forbidden:
        if value in finalizer_text:
            raise RuntimeError(f"Finalizer still deletes installed-system state: {value}")

    if "/usr/bin/apt-get purge --yes --no-install-recommends" not in finalizer_text:
        raise RuntimeError("Finalizer does not purge live-installer packages")

    required = (
        rootfs / "usr/bin/apt-get",
        rootfs / "usr/bin/dpkg",
        rootfs / "usr/bin/tos",
        rootfs / "usr/lib/terminalos/tos-backend",
        rootfs / "etc/apt/sources.list",
        rootfs / "var/lib/dpkg/status",
        rootfs / "etc/systemd/system/gdm.service.d/80-terminalos-install-mode.conf",
    )

    for path in required:
        if not path.exists():
            raise RuntimeError(f"Required installed-system path is missing: {path}")

    if (rootfs / "etc/apt/sources.list").read_text() != DEBIAN_SOURCES:
        raise RuntimeError("Debian update and security repositories are not exact")

    login_text = (rootfs / "usr/local/libexec/terminalos-install-mode").read_text()

    if "AutomaticLoginEnable\", \"true" not in login_text:
        raise RuntimeError("Live autologin was not enabled")

    if "terminalos.install=1" in login_text:
        raise RuntimeError("Live autologin is still limited to installer mode")

    sudoers = (rootfs / "etc/sudoers.d/terminalos-installer").read_text()

    if "ira ALL=(ALL:ALL) NOPASSWD: ALL" not in sudoers:
        raise RuntimeError("Passwordless live sudo rule is missing")

    if "\nterminal " in f"\n{sudoers}":
        raise RuntimeError("Dormant terminal account is present in live sudoers")

    remove_users = (
        rootfs / "etc/calamares/modules/shellprocess_remove_live_user.conf"
    ).read_text()

    for username in ("ira", "terminal"):
        if username not in remove_users:
            raise RuntimeError(f"Installer does not remove temporary account {username}")

    finalizer_module = (
        rootfs / "etc/calamares/modules/shellprocess_terminalos_finalize.conf"
    )
    if finalizer_module.read_text() != FINALIZER_MODULE:
        raise RuntimeError("Installer finalizer module is not the audited configuration")

    if (rootfs / "etc/machine-id").read_bytes():
        raise RuntimeError("Fixed live machine-id remains")

    dbus_machine_id = rootfs / "var/lib/dbus/machine-id"
    if not dbus_machine_id.is_symlink() or os.readlink(dbus_machine_id) != "/etc/machine-id":
        raise RuntimeError("D-Bus machine-id is not linked to the per-boot system identity")

    for path in (rootfs / "etc/ppp/pap-secrets", rootfs / "etc/ppp/chap-secrets"):
        if path.exists() and path.stat().st_mode & 0o077:
            raise RuntimeError(f"PPP secret file is readable by non-root users: {path}")

    forbidden_residue = (
        rootfs / ".random-seed",
        rootfs / "var/lib/systemd/random-seed",
        rootfs / "var/lib/urandom/random-seed",
        rootfs / "root/.bash_history",
        rootfs / "home/ira/.bash_history",
        rootfs / "home/terminal/.bash_history",
        rootfs / "var/cache/apt/pkgcache.bin",
        rootfs / "var/cache/apt/srcpkgcache.bin",
    )

    for path in forbidden_residue:
        if path.exists():
            raise RuntimeError(f"Generated or sensitive residue remains: {path}")


def patch_rootfs(rootfs: Path) -> None:
    if not (rootfs / "etc/passwd").is_file():
        raise RuntimeError(f"Not a Linux root filesystem: {rootfs}")

    for database in ("shadow", "shadow-"):
        for username in ("ira", "terminal"):
            replace_account_password(rootfs / "etc" / database, username)

    for database in ("group", "group-", "gshadow", "gshadow-"):
        remove_supplementary_membership(rootfs / "etc" / database, "terminal")

    write_executable(
        rootfs / "usr/local/libexec/terminalos-finalize-installed-system",
        FINALIZER,
    )
    write_executable(
        rootfs / "usr/local/libexec/terminalos-install-mode",
        LIVE_LOGIN,
    )
    (rootfs / "etc/calamares/modules/shellprocess_remove_live_user.conf").write_text(
        REMOVE_LIVE_USERS
    )
    (rootfs / "etc/calamares/modules/shellprocess_terminalos_finalize.conf").write_text(
        FINALIZER_MODULE
    )

    sudoers = rootfs / "etc/sudoers.d/terminalos-installer"
    sudoers.chmod(0o640)
    sudoers.write_text(LIVE_SUDOERS)
    sudoers.chmod(0o440)

    sources = rootfs / "etc/apt/sources.list"
    sources.write_text(DEBIAN_SOURCES)
    sources.chmod(0o644)

    for path in (rootfs / "etc/ppp/pap-secrets", rootfs / "etc/ppp/chap-secrets"):
        if path.exists():
            path.chmod(0o600)

    reset_machine_identity(rootfs)
    scrub_build_residue(rootfs)
    remove_public_random_seeds(rootfs)
    update_package_md5sums(
        rootfs,
        (
            "etc/calamares/modules/shellprocess_remove_live_user.conf",
            "etc/calamares/modules/shellprocess_terminalos_finalize.conf",
            "etc/calamares/settings.conf",
            "etc/sudoers.d/terminalos-installer",
            "usr/local/libexec/terminalos-finalize-installed-system",
            "usr/local/libexec/terminalos-install-mode",
        ),
    )
    verify(rootfs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the audited TerminalOS release repairs to an extracted rootfs"
    )
    parser.add_argument("rootfs", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    patch_rootfs(arguments.rootfs.resolve())
    print("TerminalOS root filesystem repair passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
