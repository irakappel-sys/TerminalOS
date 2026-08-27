#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASE_TAG="v1.0.0"
readonly ASSET_NAME="TerminalOS-1.0.0.iso"
readonly TEMP_ASSET_NAME="TerminalOS-1.0.0-repaired.iso"
readonly EXPECTED_ORIGINAL_SIZE="1532930048"
readonly EXPECTED_ORIGINAL_SHA256="ea59960de319624eae5cad8df070b0f346224d8b3888427bd02f7aa7e3794311"
readonly REPAIR_MARKER="terminalos-v1.0.0-repair"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

github_token="$GH_TOKEN"
unset GH_TOKEN

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner_temp="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "${runner_temp%/}/terminalos-release-repair.XXXXXX")"
download_dir="$work_dir/download"
files_dir="$work_dir/files"
rootfs="$work_dir/rootfs"
verify_dir="$work_dir/verify"
initramfs_verify_dir="$work_dir/initramfs-verify"

mkdir -p \
    "$download_dir" \
    "$files_dir/boot" \
    "$files_dir/live" \
    "$verify_dir" \
    "$initramfs_verify_dir"

original_iso="$download_dir/$ASSET_NAME"
squashfs="$files_dir/live/filesystem.squashfs"
initramfs="$files_dir/boot/initrd.img"
initramfs_unpadded="$work_dir/initrd.img.unpadded"
filesystem_size_file="$files_dir/live/filesystem.size"
md5_manifest="$files_dir/md5sum.txt"
new_squashfs="$work_dir/filesystem.squashfs.new"
temporary_iso="$work_dir/$TEMP_ASSET_NAME"
final_iso="$work_dir/$ASSET_NAME"
processors="$(nproc)"
if (( processors > 8 )); then
    processors=8
fi

fail() {
    printf 'release repair: %s\n' "$*" >&2
    exit 1
}

github_cli() {
    GH_TOKEN="$github_token" gh "$@"
}

remove_exact_tree() {
    local target="$1"

    case "$target" in
        "$work_dir"/*)
            sudo find "$target" -xdev -depth -delete
            ;;
        *)
            fail "refusing to remove a path outside the repair directory: $target"
            ;;
    esac
}

asset_json() {
    local name="$1"

    github_cli api "repos/$GITHUB_REPOSITORY/releases/tags/$RELEASE_TAG" \
        --jq ".assets[] | select(.name == \"$name\")"
}

verify_remote_asset() {
    local name="$1"
    local expected_size="$2"
    local expected_sha="$3"
    local json=""
    local digest=""
    local size=""

    for _ in 1 2 3 4 5 6; do
        json="$(asset_json "$name")"

        if [[ -n "$json" ]]; then
            size="$(jq -r '.size' <<<"$json")"
            digest="$(jq -r '.digest // empty' <<<"$json")"

            if [[ "$size" == "$expected_size" && "$digest" == "sha256:$expected_sha" ]]; then
                printf 'Verified remote asset %s (%s bytes, sha256:%s)\n' \
                    "$name" "$size" "$expected_sha"
                return 0
            fi
        fi

        sleep 5
    done

    fail "remote verification failed for $name (size=$size digest=$digest)"
}

printf 'Repair workspace: %s\n' "$work_dir"
df -h "$runner_temp"

github_cli release download "$RELEASE_TAG" \
    --repo "$GITHUB_REPOSITORY" \
    --pattern "$ASSET_NAME" \
    --dir "$download_dir"

[[ "$(stat -c '%s' "$original_iso")" == "$EXPECTED_ORIGINAL_SIZE" ]] || \
    fail "published ISO size does not match the audited source"
printf '%s  %s\n' "$EXPECTED_ORIGINAL_SHA256" "$original_iso" | sha256sum -c -

xorriso -osirrox on \
    -indev "$original_iso" \
    -extract /live/filesystem.squashfs "$squashfs" \
    -extract /boot/initrd.img "$initramfs" \
    -extract /live/filesystem.size "$filesystem_size_file" \
    -extract /md5sum.txt "$md5_manifest"

chmod u+w "$squashfs" "$initramfs" "$filesystem_size_file" "$md5_manifest"

readonly squashfs_extent_size="$(stat -c '%s' "$squashfs")"
readonly initramfs_extent_size="$(stat -c '%s' "$initramfs")"
readonly filesystem_size_extent_size="$(stat -c '%s' "$filesystem_size_file")"
readonly md5_manifest_extent_size="$(stat -c '%s' "$md5_manifest")"

sudo unsquashfs -processors "$processors" -d "$rootfs" "$squashfs"

policy_paths=(
    "$rootfs/etc/calamares/modules/users.conf"
    "$rootfs/etc/calamares/settings.conf"
    "$rootfs/etc/security/pwquality.conf"
    "$rootfs/etc/security/pwquality.conf.d/99-terminalos-permissive.conf"
)

sudo sha256sum "${policy_paths[@]}" > "$work_dir/password-policy.before.sha256"
sudo python3 "$repo_root/tools/patch-rootfs.py" "$rootfs"

sudo chroot "$rootfs" /usr/bin/apt-get --version
sudo chroot "$rootfs" /usr/bin/dpkg --version
sudo chroot "$rootfs" /usr/bin/tos --version
sudo chroot "$rootfs" /usr/bin/tos info bash > "$work_dir/tos-info-bash.txt"

# apt-cache may regenerate its disposable binary caches during the live check.
sudo python3 "$repo_root/tools/patch-rootfs.py" "$rootfs"
sudo sha256sum "${policy_paths[@]}" > "$work_dir/password-policy.after.sha256"
diff -u \
    "$work_dir/password-policy.before.sha256" \
    "$work_dir/password-policy.after.sha256"

rootfs_size="$(sudo du -sx --block-size=1 "$rootfs" | awk '{print $1}')"
python3 - "$filesystem_size_file" "$rootfs_size" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(f"{sys.argv[2]}\n")
PY

[[ "$(stat -c '%s' "$filesystem_size_file")" == "$filesystem_size_extent_size" ]] || \
    fail "filesystem.size no longer fits its original ISO extent"

sudo env SOURCE_DATE_EPOCH=1787192833 \
    mksquashfs "$rootfs" "$new_squashfs" \
    -comp xz \
    -Xbcj x86 \
    -b 1048576 \
    -noappend \
    -processors "$processors"

new_squashfs_size="$(stat -c '%s' "$new_squashfs")"
(( new_squashfs_size <= squashfs_extent_size )) || \
    fail "repaired SquashFS exceeds its original extent: $new_squashfs_size > $squashfs_extent_size"

sudo truncate -s "$squashfs_extent_size" "$new_squashfs"
sudo chown "$(id -u):$(id -g)" "$new_squashfs"
unlink "$squashfs"
mv "$new_squashfs" "$squashfs"

python3 "$repo_root/tools/patch-initramfs.py" \
    --preserve-size \
    --unpadded-copy "$initramfs_unpadded" \
    "$initramfs"
[[ "$(stat -c '%s' "$initramfs")" == "$initramfs_extent_size" ]] || \
    fail "repaired initramfs no longer fits its original ISO extent"

unmkinitramfs "$initramfs_unpadded" "$initramfs_verify_dir"
if sudo find "$initramfs_verify_dir" -type f \
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

remove_exact_tree "$rootfs"
df -h "$runner_temp"

python3 "$repo_root/tools/patch-iso-files.py" \
    --original "$original_iso" \
    --output "$temporary_iso" \
    --replace /live/filesystem.squashfs "$squashfs" \
    --replace /boot/initrd.img "$initramfs" \
    --replace /live/filesystem.size "$filesystem_size_file" \
    --replace /md5sum.txt "$md5_manifest"

[[ "$(stat -c '%s' "$temporary_iso")" == "$EXPECTED_ORIGINAL_SIZE" ]] || \
    fail "final ISO size changed"

mkdir -p "$verify_dir/boot" "$verify_dir/live"
xorriso -osirrox on \
    -indev "$temporary_iso" \
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

shadow_entry="$(sudo unsquashfs -cat "$squashfs" etc/shadow | awk -F: '$1 == "ira" { print $2 }')"
[[ "$shadow_entry" == "!" ]] || fail "the live account still has an embedded password hash"

finalizer_text="$(sudo unsquashfs -cat "$squashfs" usr/local/libexec/terminalos-finalize-installed-system)"
for forbidden in /var/lib/dpkg /var/lib/apt /etc/apt /usr/bin/apt-get /usr/bin/dpkg libapt-pkg libdpkg; do
    [[ "$finalizer_text" != *"$forbidden"* ]] || \
        fail "installer finalizer still deletes package management: $forbidden"
done

for required in usr/bin/apt-get usr/bin/dpkg usr/bin/tos usr/lib/terminalos/tos-backend etc/apt/sources.list var/lib/dpkg/status; do
    sudo unsquashfs -cat "$squashfs" "$required" > /dev/null
done

login_script="$work_dir/terminalos-install-mode"
sudo unsquashfs -cat "$squashfs" usr/local/libexec/terminalos-install-mode > "$login_script"
sh -n "$login_script"
grep -q 'AutomaticLogin.*ira' "$login_script"
! grep -q 'terminalos.install=1' "$login_script"

sudoers_file="$work_dir/terminalos-installer.sudoers"
sudo unsquashfs -cat "$squashfs" etc/sudoers.d/terminalos-installer > "$sudoers_file"
grep -q 'ira ALL=(ALL:ALL) NOPASSWD: ALL' "$sudoers_file"
sudo visudo -cf "$sudoers_file"

remove_live_user="$(sudo unsquashfs -cat "$squashfs" etc/calamares/modules/shellprocess_remove_live_user.conf)"
[[ "$remove_live_user" == *"userdel -f -r ira"* ]] || \
    fail "installer no longer removes the temporary live account"

for image in "$original_iso" "$temporary_iso"; do
    report="$work_dir/$(basename "$image").boot-report"
    xorriso -indev "$image" \
        -report_el_torito plain \
        -report_system_area plain 2>&1 | \
        grep -E '^(Boot record|El Torito|System area|Partition offset|MBR|GPT|APM)' \
        > "$report"
done
diff -u \
    "$work_dir/$(basename "$original_iso").boot-report" \
    "$work_dir/$(basename "$temporary_iso").boot-report"

secure_boot_paths=(
    /efi.img
    /efi/boot/bootx64.efi
    /efi_/boot/bootx64.efi
    /boot/grub/i386-pc/eltorito.img
)

mkdir -p "$work_dir/secure-original" "$work_dir/secure-repaired"
for iso_path in "${secure_boot_paths[@]}"; do
    safe_name="${iso_path//\//_}"
    xorriso -osirrox on -indev "$original_iso" \
        -extract "$iso_path" "$work_dir/secure-original/$safe_name"
    xorriso -osirrox on -indev "$temporary_iso" \
        -extract "$iso_path" "$work_dir/secure-repaired/$safe_name"
    cmp \
        "$work_dir/secure-original/$safe_name" \
        "$work_dir/secure-repaired/$safe_name"
done

final_size="$(stat -c '%s' "$temporary_iso")"
final_sha256="$(sha256sum "$temporary_iso" | awk '{print $1}')"
[[ "$final_sha256" != "$EXPECTED_ORIGINAL_SHA256" ]] || \
    fail "repair unexpectedly produced the original ISO checksum"

printf 'Uploading recoverable repaired asset before replacing the published filename.\n'
github_cli release upload "$RELEASE_TAG" "$temporary_iso" \
    --repo "$GITHUB_REPOSITORY" \
    --clobber
verify_remote_asset "$TEMP_ASSET_NAME" "$final_size" "$final_sha256"

original_asset="$(asset_json "$ASSET_NAME")"
[[ -n "$original_asset" ]] || fail "published release asset disappeared before replacement"
original_asset_id="$(jq -r '.id' <<<"$original_asset")"
github_cli api --method DELETE \
    "repos/$GITHUB_REPOSITORY/releases/assets/$original_asset_id"

mv "$temporary_iso" "$final_iso"
github_cli release upload "$RELEASE_TAG" "$final_iso" --repo "$GITHUB_REPOSITORY"
verify_remote_asset "$ASSET_NAME" "$final_size" "$final_sha256"

temporary_asset="$(asset_json "$TEMP_ASSET_NAME")"
if [[ -n "$temporary_asset" ]]; then
    temporary_asset_id="$(jq -r '.id' <<<"$temporary_asset")"
    github_cli api --method DELETE \
        "repos/$GITHUB_REPOSITORY/releases/assets/$temporary_asset_id"
fi

release_body="$work_dir/release-body.md"
current_body="$(github_cli release view "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --json body --jq .body)"
python3 - "$release_body" "$current_body" "$final_size" "$final_sha256" "$REPAIR_MARKER" <<'PY'
from pathlib import Path
import re
import sys

output = Path(sys.argv[1])
body = sys.argv[2]
size = sys.argv[3]
digest = sys.argv[4]
marker = sys.argv[5]
pattern = rf"\n?<!-- {re.escape(marker)} -->.*?<!-- /{re.escape(marker)} -->\n?"
body = re.sub(pattern, "\n", body, flags=re.DOTALL).rstrip()
body, size_count = re.subn(
    r"(?m)^Size: `?\d+`? bytes$",
    f"Size: {size} bytes",
    body,
)
body, digest_count = re.subn(
    r"(?m)^SHA-256: `?[0-9a-f]{64}`?$",
    f"SHA-256: {digest}",
    body,
)

missing_metadata = []
if size_count == 0:
    missing_metadata.append(f"Size: {size} bytes")
if digest_count == 0:
    missing_metadata.append(f"SHA-256: {digest}")
if missing_metadata:
    body = (f"{body}\n\n" + "\n\n".join(missing_metadata)).strip()

repair = f"""<!-- {marker} -->
### Repaired image — 2026-08-27

- Preserves APT, DPKG, repositories, and `tos` after installation.
- Removes the embedded live-user password hash; live sessions log in without a password, while the installer still creates the installed user's password.
- Removes the fixed public random seed from the initramfs.
- Leaves the existing installer password policy and Secure Boot behavior unchanged.
<!-- /{marker} -->"""
output.write_text(f"{body}\n\n{repair}\n" if body else f"{repair}\n")
PY
github_cli release edit "$RELEASE_TAG" \
    --repo "$GITHUB_REPOSITORY" \
    --notes-file "$release_body"

python3 - "$repo_root/README.md" "$final_size" "$final_sha256" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
size = sys.argv[2]
digest = sys.argv[3]
text = path.read_text()
text, size_count = re.subn(r"(?m)^- Size: \d+ bytes$", f"- Size: {size} bytes", text)
text, digest_count = re.subn(
    r"(?m)^- SHA-256: `[0-9a-f]{64}`$",
    f"- SHA-256: `{digest}`",
    text,
)

if size_count != 1 or digest_count != 1:
    raise RuntimeError("README release metadata was not uniquely identifiable")

path.write_text(text)
PY

git -C "$repo_root" config user.name "github-actions[bot]"
git -C "$repo_root" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git -C "$repo_root" add README.md
git -C "$repo_root" commit -m "Update repaired TerminalOS 1.0.0 checksum"
git -C "$repo_root" push

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        printf '## TerminalOS 1.0.0 release repaired\n\n'
        printf -- '- Size: `%s` bytes\n' "$final_size"
        printf -- '- SHA-256: `%s`\n' "$final_sha256"
        printf -- '- Published asset: `%s`\n' "$ASSET_NAME"
    } >> "$GITHUB_STEP_SUMMARY"
fi

printf 'REPAIRED_SIZE=%s\n' "$final_size"
printf 'REPAIRED_SHA256=%s\n' "$final_sha256"
