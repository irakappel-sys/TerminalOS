#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


FINALIZER = """#!/bin/sh
set -eu

LOG=/var/log/terminalos-installer-finalize.log
mkdir -p /var/log
exec >>"$LOG" 2>&1

echo "===== $(date --iso-8601=seconds) ====="
echo "Removing live-installer artifacts while preserving package management."

rm -f \\
    /usr/local/bin/terminalos-installer \\
    /usr/local/bin/terminalos-installer-autostart \\
    /usr/local/libexec/terminalos-calamares-root \\
    /usr/local/libexec/terminalos-install-mode \\
    /usr/share/applications/terminalos-installer.desktop \\
    /usr/share/applications/calamares.desktop \\
    /usr/share/applications/calamares.desktop.orig \\
    /usr/share/applications/install-debian.desktop \\
    /usr/bin/calamares-install-debian \\
    /usr/share/applications/calamares-install-debian.desktop \\
    /usr/share/pixmaps/install-debian.png \\
    /etc/xdg/autostart/terminalos-installer-autostart.desktop \\
    /etc/xdg/autostart/calamares-desktop-icon.desktop \\
    /etc/systemd/system/gdm.service.d/80-terminalos-install-mode.conf \\
    /etc/systemd/system/terminalos-install-mode.service \\
    /etc/systemd/system/graphical.target.wants/terminalos-install-mode.service \\
    /etc/sudoers.d/terminalos-installer \\
    /etc/security/pwquality.conf.d/99-terminalos-permissive.conf \\
    /var/lib/AccountsService/users/ira

rm -rf \\
    /etc/calamares \\
    /root/.cache/calamares

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


def lock_account(shadow_path: Path, username: str) -> None:
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


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def scrub_build_residue(rootfs: Path) -> None:
    generated_files = (
        rootfs / "root/.bash_history",
        rootfs / "root/.lesshst",
        rootfs / "home/ira/.bash_history",
        rootfs / "home/ira/.lesshst",
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


def verify(rootfs: Path) -> None:
    for name in ("shadow", "shadow-"):
        path = rootfs / f"etc/{name}"

        if not path.exists():
            continue

        entry = next(
            (line for line in path.read_text().splitlines() if line.startswith("ira:")),
            None,
        )

        if entry is None or entry.split(":", 2)[1] != "!":
            raise RuntimeError(f"Live account is not locked in {path}")

    finalizer = rootfs / "usr/local/libexec/terminalos-finalize-installed-system"
    finalizer_text = finalizer.read_text()
    forbidden = (
        "/var/lib/dpkg",
        "/var/lib/apt",
        "/etc/apt",
        "/usr/bin/apt-get",
        "/usr/bin/dpkg",
        "libapt-pkg",
        "libdpkg",
    )

    for value in forbidden:
        if value in finalizer_text:
            raise RuntimeError(f"Finalizer still deletes package state: {value}")

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
            raise RuntimeError(f"Required package-management path is missing: {path}")

    login_text = (rootfs / "usr/local/libexec/terminalos-install-mode").read_text()

    if "AutomaticLoginEnable\", \"true" not in login_text:
        raise RuntimeError("Live autologin was not enabled")

    if "terminalos.install=1" in login_text:
        raise RuntimeError("Live autologin is still limited to installer mode")

    sudoers = (rootfs / "etc/sudoers.d/terminalos-installer").read_text()

    if "ira ALL=(ALL:ALL) NOPASSWD: ALL" not in sudoers:
        raise RuntimeError("Passwordless live sudo rule is missing")

    forbidden_residue = (
        rootfs / ".random-seed",
        rootfs / "var/lib/systemd/random-seed",
        rootfs / "var/lib/urandom/random-seed",
        rootfs / "root/.bash_history",
        rootfs / "home/ira/.bash_history",
        rootfs / "var/cache/apt/pkgcache.bin",
        rootfs / "var/cache/apt/srcpkgcache.bin",
    )

    for path in forbidden_residue:
        if path.exists():
            raise RuntimeError(f"Generated or sensitive residue remains: {path}")


def patch_rootfs(rootfs: Path) -> None:
    if not (rootfs / "etc/passwd").is_file():
        raise RuntimeError(f"Not a Linux root filesystem: {rootfs}")

    lock_account(rootfs / "etc/shadow", "ira")
    lock_account(rootfs / "etc/shadow-", "ira")
    write_executable(
        rootfs / "usr/local/libexec/terminalos-finalize-installed-system",
        FINALIZER,
    )
    write_executable(
        rootfs / "usr/local/libexec/terminalos-install-mode",
        LIVE_LOGIN,
    )

    sudoers = rootfs / "etc/sudoers.d/terminalos-installer"
    sudoers.chmod(0o640)
    sudoers.write_text(LIVE_SUDOERS)
    sudoers.chmod(0o440)

    scrub_build_residue(rootfs)
    remove_public_random_seeds(rootfs)
    verify(rootfs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the TerminalOS 1.0.0 release repairs to an extracted rootfs"
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
