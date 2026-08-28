#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASE_TAG="v1.0.0"
readonly ASSET_NAME="TerminalOS-1.0.0.iso"
readonly EXPECTED_SOURCE_SIZE="1532930048"
readonly EXPECTED_SOURCE_SHA256="c35694e69921329882d079ecc68c50e50787ff7d615b0d255af840c168c104c8"
readonly SOURCE_DATE_EPOCH="1787192833"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner_temp="${RUNNER_TEMP:-/tmp}"
output_dir="${OUTPUT_DIR:-$repo_root/dist}"
work_dir="$(mktemp -d "${runner_temp%/}/terminalos-release-build.XXXXXX")"
download_dir="$work_dir/download"
files_dir="$work_dir/files"
rootfs="$work_dir/rootfs"
verify_dir="$work_dir/verify"
initramfs_verify_dir="$work_dir/initramfs-verify"
mounted_paths=()

mkdir -p \
    "$download_dir" \
    "$files_dir/boot" \
    "$files_dir/live" \
    "$verify_dir" \
    "$initramfs_verify_dir" \
    "$output_dir"

source_iso="$download_dir/$ASSET_NAME"
squashfs="$files_dir/live/filesystem.squashfs"
initramfs="$files_dir/boot/initrd.img"
initramfs_unpadded="$work_dir/initrd.img.unpadded"
filesystem_size_file="$files_dir/live/filesystem.size"
md5_manifest="$files_dir/md5sum.txt"
new_squashfs="$work_dir/filesystem.squashfs.new"
candidate_iso="$work_dir/$ASSET_NAME"
final_iso="$output_dir/$ASSET_NAME"
processors="$(nproc)"
if (( processors > 8 )); then
    processors=8
fi

fail() {
    printf 'release build: %s\n' "$*" >&2
    exit 1
}

as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo "$@"
    fi
}

unmount_rootfs() {
    local index

    for (( index=${#mounted_paths[@]} - 1; index >= 0; index-- )); do
        if mountpoint -q "${mounted_paths[index]}"; then
            as_root umount --recursive "${mounted_paths[index]}" || \
                as_root umount --lazy "${mounted_paths[index]}"
        fi
    done

    mounted_paths=()
}

remove_exact_tree() {
    local target="$1"

    case "$target" in
        "$work_dir"|"$work_dir"/*)
            if [[ -e "$target" ]]; then
                as_root find "$target" -xdev -depth -delete
            fi
            ;;
        *)
            fail "refusing to remove a path outside the build directory: $target"
            ;;
    esac
}

cleanup() {
    local exit_status=$?
    set +e
    unmount_rootfs

    if [[ "${KEEP_WORK_DIR:-0}" != "1" ]]; then
        remove_exact_tree "$work_dir"
    else
        printf 'Preserved build workspace: %s\n' "$work_dir" >&2
    fi

    exit "$exit_status"
}
trap cleanup EXIT INT TERM

rootfs_command() {
    as_root chroot "$rootfs" /usr/bin/env -i \
        HOME=/root \
        LANG=C \
        LC_ALL=C \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        DEBIAN_FRONTEND=noninteractive \
        APT_LISTCHANGES_FRONTEND=none \
        "$@"
}

hash_identity() {
    local output="$1"
    local package

    {
        for package in \
            terminalos-base \
            terminalos-branding \
            terminalos-network-config \
            terminalos-package-manager
        do
            cat "$rootfs/var/lib/dpkg/info/$package.list"
        done
    } | sort -u | while IFS= read -r absolute_path; do
        [[ "$absolute_path" == "/etc/apt/sources.list" ]] && continue
        path="$rootfs$absolute_path"

        if [[ -L "$path" ]]; then
            printf 'link  %s  %s\n' "$absolute_path" "$(readlink "$path")"
        elif [[ -f "$path" ]]; then
            digest="$(sha256sum "$path" | awk '{print $1}')"
            printf 'file  %s  %s\n' "$absolute_path" "$digest"
        fi
    done > "$output"
}

upgrade_rootfs() {
    local policy_rc="$rootfs/usr/sbin/policy-rc.d"
    local policy_backup="$work_dir/policy-rc.d.original"
    local resolver="$rootfs/etc/resolv.conf"
    local resolver_backup="$work_dir/resolv.conf.original"
    local policy_existed=0
    local resolver_existed=0
    local pending_upgrades="$work_dir/pending-upgrades.txt"
    local dpkg_audit="$work_dir/dpkg-audit.txt"

    if [[ -e "$policy_rc" || -L "$policy_rc" ]]; then
        cp --archive --no-dereference "$policy_rc" "$policy_backup"
        policy_existed=1
    fi

    if [[ -e "$resolver" || -L "$resolver" ]]; then
        cp --archive --no-dereference "$resolver" "$resolver_backup"
        resolver_existed=1
    fi

    as_root rm -f "$policy_rc" "$resolver"
    printf '#!/bin/sh\nexit 101\n' > "$work_dir/policy-rc.d"
    chmod 0755 "$work_dir/policy-rc.d"
    as_root install -m 0755 "$work_dir/policy-rc.d" "$policy_rc"
    as_root install -m 0644 /etc/resolv.conf "$resolver"

    as_root mount --rbind /dev "$rootfs/dev"
    mounted_paths+=("$rootfs/dev")
    as_root mount --make-rslave "$rootfs/dev"
    as_root mount -t proc proc "$rootfs/proc"
    mounted_paths+=("$rootfs/proc")
    as_root mount --rbind /sys "$rootfs/sys"
    mounted_paths+=("$rootfs/sys")
    as_root mount --make-rslave "$rootfs/sys"

    rootfs_command /usr/bin/apt-get update
    rootfs_command /usr/bin/apt-get \
        --yes \
        --no-install-recommends \
        -o Dpkg::Use-Pty=0 \
        -o Dpkg::Options::=--force-confold \
        full-upgrade
    rootfs_command /usr/bin/apt-get check
    rootfs_command /usr/bin/dpkg --audit > "$dpkg_audit"
    [[ ! -s "$dpkg_audit" ]] || {
        cat "$dpkg_audit" >&2
        fail "DPKG reports an incomplete package state after the security upgrade"
    }

    rootfs_command /usr/bin/apt-get --simulate full-upgrade > "$pending_upgrades"
    if grep -q '^Inst ' "$pending_upgrades"; then
        cat "$pending_upgrades" >&2
        fail "packages remain upgradeable immediately after the security upgrade"
    fi

    rootfs_command /usr/bin/apt-get clean
    unmount_rootfs

    as_root rm -f "$policy_rc" "$resolver"
    if (( policy_existed )); then
        as_root mv "$policy_backup" "$policy_rc"
    fi
    if (( resolver_existed )); then
        as_root mv "$resolver_backup" "$resolver"
    fi

    if [[ -d "$rootfs/var/lib/apt/lists" ]]; then
        as_root find "$rootfs/var/lib/apt/lists" -mindepth 1 -xdev -depth -delete
    fi
}

printf 'Build workspace: %s\n' "$work_dir"
df -h "$runner_temp"

source_url="https://github.com/$GITHUB_REPOSITORY/releases/download/$RELEASE_TAG/$ASSET_NAME"
curl \
    --fail \
    --location \
    --proto '=https' \
    --retry 4 \
    --retry-all-errors \
    --show-error \
    --silent \
    --output "$source_iso" \
    "$source_url"

[[ "$(stat -c '%s' "$source_iso")" == "$EXPECTED_SOURCE_SIZE" ]] || \
    fail "published ISO size does not match the audited source"
printf '%s  %s\n' "$EXPECTED_SOURCE_SHA256" "$source_iso" | sha256sum -c -

xorriso -osirrox on \
    -indev "$source_iso" \
    -extract /live/filesystem.squashfs "$squashfs" \
    -extract /boot/initrd.img "$initramfs" \
    -extract /live/filesystem.size "$filesystem_size_file" \
    -extract /md5sum.txt "$md5_manifest"

chmod u+w "$squashfs" "$initramfs" "$filesystem_size_file" "$md5_manifest"

squashfs_extent_size="$(stat -c '%s' "$squashfs")"
initramfs_extent_size="$(stat -c '%s' "$initramfs")"
filesystem_size_extent_size="$(stat -c '%s' "$filesystem_size_file")"
md5_manifest_extent_size="$(stat -c '%s' "$md5_manifest")"
readonly \
    squashfs_extent_size \
    initramfs_extent_size \
    filesystem_size_extent_size \
    md5_manifest_extent_size

as_root unsquashfs -processors "$processors" -d "$rootfs" "$squashfs"

policy_paths=(
    "$rootfs/etc/calamares/modules/users.conf"
    "$rootfs/etc/calamares/settings.conf"
    "$rootfs/etc/security/pwquality.conf"
    "$rootfs/etc/security/pwquality.conf.d/99-terminalos-permissive.conf"
)

as_root sha256sum "${policy_paths[@]}" > "$work_dir/password-policy.before.sha256"
hash_identity "$work_dir/terminalos-identity.before.sha256"

as_root python3 "$repo_root/tools/patch-rootfs.py" "$rootfs"
upgrade_rootfs
as_root python3 "$repo_root/tools/patch-rootfs.py" "$rootfs"

as_root sha256sum "${policy_paths[@]}" > "$work_dir/password-policy.after.sha256"
diff -u \
    "$work_dir/password-policy.before.sha256" \
    "$work_dir/password-policy.after.sha256"

hash_identity "$work_dir/terminalos-identity.after.sha256"
diff -u \
    "$work_dir/terminalos-identity.before.sha256" \
    "$work_dir/terminalos-identity.after.sha256"

debsecan \
    --suite trixie \
    --status "$rootfs/var/lib/dpkg/status" \
    --only-fixed > "$work_dir/debsecan-fixed.txt"
tracker_advisories="$(awk '{print $1}' "$work_dir/debsecan-fixed.txt" | sort -u | sed '/^$/d' | wc -l)"
printf 'Debian tracker advisories without a newer package in the configured Trixie repositories: %s\n' \
    "$tracker_advisories"

for account_database in shadow shadow-; do
    for account in ira terminal; do
        password_field="$(awk -F: -v account="$account" '$1 == account { print $2 }' "$rootfs/etc/$account_database")"
        [[ "$password_field" == "!" ]] || \
            fail "$account still has password material in $account_database"
    done
done

for group_database in group group- gshadow gshadow-; do
    if awk -F: '$4 ~ /(^|,)terminal(,|$)/ { found=1 } END { exit !found }' "$rootfs/etc/$group_database"; then
        fail "terminal retains supplementary access in $group_database"
    fi
done

grep -Fxq 'deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware' \
    "$rootfs/etc/apt/sources.list"
grep -Fxq 'deb https://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware' \
    "$rootfs/etc/apt/sources.list"
grep -Fxq 'deb https://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware' \
    "$rootfs/etc/apt/sources.list"
[[ "$(wc -l < "$rootfs/etc/apt/sources.list")" == "3" ]] || \
    fail "unexpected entries exist in the Debian source list"

[[ ! -s "$rootfs/etc/machine-id" ]] || fail "fixed live machine-id remains"
[[ -L "$rootfs/var/lib/dbus/machine-id" ]] || fail "D-Bus machine-id is not a symlink"
[[ "$(readlink "$rootfs/var/lib/dbus/machine-id")" == "/etc/machine-id" ]] || \
    fail "D-Bus machine-id has an unexpected target"

for secret_file in "$rootfs/etc/ppp/pap-secrets" "$rootfs/etc/ppp/chap-secrets"; do
    [[ "$(stat -c '%a' "$secret_file")" == "600" ]] || \
        fail "PPP secret file permissions are not 0600: $secret_file"
done

as_root chroot "$rootfs" /usr/bin/dpkg --verify terminalos-installer-config > \
    "$work_dir/installer-package-verify.txt"
[[ ! -s "$work_dir/installer-package-verify.txt" ]] || {
    cat "$work_dir/installer-package-verify.txt" >&2
    fail "patched installer files do not match DPKG integrity metadata"
}

rootfs_size="$(as_root du -sx --block-size=1 "$rootfs" | awk '{print $1}')"
python3 - "$filesystem_size_file" "$rootfs_size" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(f"{sys.argv[2]}\n")
PY

[[ "$(stat -c '%s' "$filesystem_size_file")" == "$filesystem_size_extent_size" ]] || \
    fail "filesystem.size no longer fits its original ISO extent"

as_root env SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    mksquashfs "$rootfs" "$new_squashfs" \
    -comp xz \
    -Xbcj x86 \
    -b 1048576 \
    -noappend \
    -processors "$processors"

new_squashfs_size="$(stat -c '%s' "$new_squashfs")"
(( new_squashfs_size <= squashfs_extent_size )) || \
    fail "repaired SquashFS exceeds its original extent: $new_squashfs_size > $squashfs_extent_size"

as_root truncate -s "$squashfs_extent_size" "$new_squashfs"
as_root chown "$(id -u):$(id -g)" "$new_squashfs"
unlink "$squashfs"
mv "$new_squashfs" "$squashfs"

python3 "$repo_root/tools/patch-initramfs.py" \
    --allow-absent \
    --preserve-size \
    --unpadded-copy "$initramfs_unpadded" \
    "$initramfs"
[[ "$(stat -c '%s' "$initramfs")" == "$initramfs_extent_size" ]] || \
    fail "repaired initramfs no longer fits its original ISO extent"

unmkinitramfs "$initramfs_unpadded" "$initramfs_verify_dir"
if as_root find "$initramfs_verify_dir" -type f \
    \( -path '*/.random-seed' \
       -o -path '*/var/lib/systemd/random-seed' \
       -o -path '*/var/lib/urandom/random-seed' \) \
    -print -quit | grep -q .; then
    fail "public random seed remained in the initramfs"
fi
remove_exact_tree "$initramfs_verify_dir"

python3 "$repo_root/tools/update-md5sum.py" "$md5_manifest" \
    --file ./live/filesystem.squashfs "$squashfs" \
    --file ./boot/initrd.img "$initramfs" \
    --file ./live/filesystem.size "$filesystem_size_file"

[[ "$(stat -c '%s' "$md5_manifest")" == "$md5_manifest_extent_size" ]] || \
    fail "updated md5sum.txt no longer fits its original ISO extent"

python3 "$repo_root/tools/patch-iso-files.py" \
    --original "$source_iso" \
    --output "$candidate_iso" \
    --replace /live/filesystem.squashfs "$squashfs" \
    --replace /boot/initrd.img "$initramfs" \
    --replace /live/filesystem.size "$filesystem_size_file" \
    --replace /md5sum.txt "$md5_manifest"

[[ "$(stat -c '%s' "$candidate_iso")" == "$EXPECTED_SOURCE_SIZE" ]] || \
    fail "final ISO size changed"

mkdir -p "$verify_dir/boot" "$verify_dir/live"
xorriso -osirrox on \
    -indev "$candidate_iso" \
    -extract /live/filesystem.squashfs "$verify_dir/live/filesystem.squashfs" \
    -extract /boot/initrd.img "$verify_dir/boot/initrd.img" \
    -extract /live/filesystem.size "$verify_dir/live/filesystem.size" \
    -extract /md5sum.txt "$verify_dir/md5sum.txt"

cmp "$squashfs" "$verify_dir/live/filesystem.squashfs"
cmp "$initramfs" "$verify_dir/boot/initrd.img"
cmp "$filesystem_size_file" "$verify_dir/live/filesystem.size"
cmp "$md5_manifest" "$verify_dir/md5sum.txt"

grep -E '  \./(live/filesystem\.(squashfs|size)|boot/initrd\.img)$' \
    "$verify_dir/md5sum.txt" > "$verify_dir/selected-md5sums.txt"
(
    cd "$verify_dir"
    md5sum -c selected-md5sums.txt
)

finalizer_file="$work_dir/terminalos-finalize-installed-system"
as_root unsquashfs -cat "$squashfs" \
    usr/local/libexec/terminalos-finalize-installed-system > "$finalizer_file"
sh -n "$finalizer_file"
grep -Fq '/usr/bin/apt-get purge --yes --no-install-recommends' "$finalizer_file"
for forbidden_cleanup in \
    'rm -rf /var/lib/dpkg' \
    'rm -rf /var/lib/apt' \
    'rm -rf /etc/apt'
do
    if grep -Fq "$forbidden_cleanup" "$finalizer_file"; then
        fail "finalizer contains forbidden cleanup: $forbidden_cleanup"
    fi
done

for required in \
    usr/bin/apt-get \
    usr/bin/dpkg \
    usr/bin/tos \
    usr/lib/terminalos/tos-backend \
    etc/apt/sources.list \
    var/lib/dpkg/status
do
    as_root unsquashfs -cat "$squashfs" "$required" > /dev/null
done

login_script="$work_dir/terminalos-install-mode"
as_root unsquashfs -cat "$squashfs" \
    usr/local/libexec/terminalos-install-mode > "$login_script"
sh -n "$login_script"
grep -q 'AutomaticLogin.*ira' "$login_script"
if grep -q 'terminalos.install=1' "$login_script"; then
    fail "live autologin is still limited to an installer kernel flag"
fi

sudoers_file="$work_dir/terminalos-installer.sudoers"
as_root unsquashfs -cat "$squashfs" \
    etc/sudoers.d/terminalos-installer > "$sudoers_file"
grep -Fxq 'ira ALL=(ALL:ALL) NOPASSWD: ALL' "$sudoers_file"
visudo -cf "$sudoers_file"

remove_live_user="$(as_root unsquashfs -cat "$squashfs" \
    etc/calamares/modules/shellprocess_remove_live_user.conf)"
[[ "$remove_live_user" == *'for user in ira terminal'* ]] || \
    fail "installer does not remove both temporary accounts"
[[ "$remove_live_user" != *'|| true'* ]] || \
    fail "installer still ignores temporary-account removal failures"

for image in "$source_iso" "$candidate_iso"; do
    report="$work_dir/$(basename "$image").boot-report"
    xorriso -indev "$image" \
        -report_el_torito plain \
        -report_system_area plain 2>&1 | \
        grep -E '^(Boot record|El Torito|System area|Partition offset|MBR|GPT|APM)' \
        > "$report"
done
diff -u \
    "$work_dir/$(basename "$source_iso").boot-report" \
    "$work_dir/$(basename "$candidate_iso").boot-report"

secure_boot_paths=(
    /efi.img
    /efi/boot/bootx64.efi
    /efi_/boot/bootx64.efi
    /boot/grub/i386-pc/eltorito.img
)

mkdir -p "$work_dir/secure-source" "$work_dir/secure-candidate"
for iso_path in "${secure_boot_paths[@]}"; do
    safe_name="${iso_path//\//_}"
    xorriso -osirrox on -indev "$source_iso" \
        -extract "$iso_path" "$work_dir/secure-source/$safe_name"
    xorriso -osirrox on -indev "$candidate_iso" \
        -extract "$iso_path" "$work_dir/secure-candidate/$safe_name"
    cmp \
        "$work_dir/secure-source/$safe_name" \
        "$work_dir/secure-candidate/$safe_name"
done

final_size="$(stat -c '%s' "$candidate_iso")"
final_sha256="$(sha256sum "$candidate_iso" | awk '{print $1}')"
[[ "$final_sha256" != "$EXPECTED_SOURCE_SHA256" ]] || \
    fail "repair unexpectedly produced the source ISO checksum"

if [[ -e "$final_iso" ]]; then
    unlink "$final_iso"
fi
mv "$candidate_iso" "$final_iso"
printf '%s  %s\n' "$final_sha256" "$ASSET_NAME" > "$output_dir/$ASSET_NAME.sha256"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        printf '## TerminalOS 1.0.0 security build passed\n\n'
        printf -- "- Size: \`%s\` bytes\n" "$final_size"
        printf -- "- SHA-256: \`%s\`\n" "$final_sha256"
        printf -- '- Password policy: unchanged\n'
        printf -- '- Secure Boot payloads: byte-for-byte unchanged\n'
        printf -- '- Configured Trixie package updates: none remaining\n'
        printf -- "- Debian tracker advisories awaiting a newer Trixie package: \`%s\`\n" \
            "$tracker_advisories"
    } >> "$GITHUB_STEP_SUMMARY"
fi

printf 'REPAIRED_ISO=%s\n' "$final_iso"
printf 'REPAIRED_SIZE=%s\n' "$final_size"
printf 'REPAIRED_SHA256=%s\n' "$final_sha256"
