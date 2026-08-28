#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASE_TAG="v1.0.0"
readonly ASSET_NAME="TerminalOS-1.0.0.iso"
readonly CHECKSUM_NAME="$ASSET_NAME.sha256"
readonly TEMP_ASSET_NAME="TerminalOS-1.0.0-security-candidate.iso"
readonly REPAIR_MARKER="terminalos-v1.0.0-security-repair"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${EXPECTED_SHA256:?EXPECTED_SHA256 is required}"
: "${EXPECTED_SIZE:?EXPECTED_SIZE is required}"

[[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
    printf 'release publish: malformed expected SHA-256\n' >&2
    exit 1
}
[[ "$EXPECTED_SIZE" =~ ^[0-9]+$ ]] || {
    printf 'release publish: malformed expected size\n' >&2
    exit 1
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
candidate_iso="${1:-$repo_root/dist/$ASSET_NAME}"
checksum_file="$(dirname "$candidate_iso")/$CHECKSUM_NAME"
runner_temp="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "${runner_temp%/}/terminalos-release-publish.XXXXXX")"
temporary_iso="$work_dir/$TEMP_ASSET_NAME"
release_body="$work_dir/release-body.md"
github_token="$GH_TOKEN"
unset GH_TOKEN

fail() {
    printf 'release publish: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local exit_status=$?
    find "$work_dir" -xdev -depth -delete
    exit "$exit_status"
}
trap cleanup EXIT INT TERM

github_cli() {
    GH_TOKEN="$github_token" gh "$@"
}

asset_json() {
    local name="$1"

    github_cli api "repos/$GITHUB_REPOSITORY/releases/tags/$RELEASE_TAG" \
        --jq ".assets[] | select(.name == \"$name\")"
}

delete_asset_if_present() {
    local name="$1"
    local json
    local asset_id

    json="$(asset_json "$name")"
    if [[ -n "$json" ]]; then
        asset_id="$(jq -r '.id' <<< "$json")"
        github_cli api --method DELETE \
            "repos/$GITHUB_REPOSITORY/releases/assets/$asset_id"
    fi
}

verify_remote_asset() {
    local name="$1"
    local expected_size="$2"
    local expected_sha="$3"
    local json=""
    local digest=""
    local size=""

    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        json="$(asset_json "$name")"

        if [[ -n "$json" ]]; then
            size="$(jq -r '.size' <<< "$json")"
            digest="$(jq -r '.digest // empty' <<< "$json")"

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

[[ -f "$candidate_iso" ]] || fail "candidate ISO is missing: $candidate_iso"
[[ -f "$checksum_file" ]] || fail "checksum sidecar is missing: $checksum_file"

actual_size="$(stat -c '%s' "$candidate_iso")"
actual_sha256="$(sha256sum "$candidate_iso" | awk '{print $1}')"
[[ "$actual_size" == "$EXPECTED_SIZE" ]] || fail "candidate size changed after the build job"
[[ "$actual_sha256" == "$EXPECTED_SHA256" ]] || fail "candidate checksum changed after the build job"
(
    cd "$(dirname "$candidate_iso")"
    sha256sum -c "$CHECKSUM_NAME"
)

if ! ln "$candidate_iso" "$temporary_iso" 2>/dev/null; then
    cp --reflink=auto "$candidate_iso" "$temporary_iso"
fi

printf 'Uploading a recoverable candidate before replacing the published filename.\n'
github_cli release upload "$RELEASE_TAG" "$temporary_iso" \
    --repo "$GITHUB_REPOSITORY" \
    --clobber
verify_remote_asset "$TEMP_ASSET_NAME" "$actual_size" "$actual_sha256"

delete_asset_if_present "$ASSET_NAME"
github_cli release upload "$RELEASE_TAG" "$candidate_iso" \
    --repo "$GITHUB_REPOSITORY"
verify_remote_asset "$ASSET_NAME" "$actual_size" "$actual_sha256"

checksum_size="$(stat -c '%s' "$checksum_file")"
checksum_sha256="$(sha256sum "$checksum_file" | awk '{print $1}')"
github_cli release upload "$RELEASE_TAG" "$checksum_file" \
    --repo "$GITHUB_REPOSITORY" \
    --clobber
verify_remote_asset "$CHECKSUM_NAME" "$checksum_size" "$checksum_sha256"
delete_asset_if_present "$TEMP_ASSET_NAME"

current_body="$(github_cli release view "$RELEASE_TAG" \
    --repo "$GITHUB_REPOSITORY" \
    --json body \
    --jq .body)"
python3 - \
    "$release_body" \
    "$current_body" \
    "$actual_size" \
    "$actual_sha256" \
    "$REPAIR_MARKER" <<'PY'
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
### Security-corrected image — 2026-08-28

- Removes all published password material from both temporary accounts.
- Keeps the live session passwordless; the installer still creates the installed account and its chosen password.
- Removes the dormant account from administrator groups and deletes both temporary accounts before user creation.
- Purges Calamares/live-only packages while preserving APT, DPKG, TerminalOS repositories, and `tos`.
- Adds Debian update/security sources and installs all currently available Debian package fixes.
- Removes fixed random seeds and fixed live machine identifiers.
- Leaves TerminalOS branding, installer password policy, and Secure Boot payloads unchanged.
<!-- /{marker} -->"""
output.write_text(f"{body}\n\n{repair}\n" if body else f"{repair}\n")
PY

github_cli release edit "$RELEASE_TAG" \
    --repo "$GITHUB_REPOSITORY" \
    --notes-file "$release_body"

python3 - "$repo_root/README.md" "$actual_size" "$actual_sha256" <<'PY'
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

readme_json="$(github_cli api "repos/$GITHUB_REPOSITORY/contents/README.md?ref=main")"
readme_blob_sha="$(jq -r '.sha' <<< "$readme_json")"
readme_content="$(base64 -w0 "$repo_root/README.md")"
github_cli api \
    --method PUT \
    "repos/$GITHUB_REPOSITORY/contents/README.md" \
    -f message='Update security-corrected TerminalOS 1.0.0 checksum' \
    -f content="$readme_content" \
    -f sha="$readme_blob_sha" \
    -f branch=main > /dev/null

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        printf '## TerminalOS 1.0.0 published and remotely verified\n\n'
        printf -- "- Size: \`%s\` bytes\n" "$actual_size"
        printf -- "- SHA-256: \`%s\`\n" "$actual_sha256"
        printf -- "- Published asset: \`%s\`\n" "$ASSET_NAME"
        printf -- "- Checksum sidecar: \`%s\`\n" "$CHECKSUM_NAME"
    } >> "$GITHUB_STEP_SUMMARY"
fi

printf 'PUBLISHED_SIZE=%s\n' "$actual_size"
printf 'PUBLISHED_SHA256=%s\n' "$actual_sha256"
