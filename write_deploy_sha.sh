#!/usr/bin/env bash
# Write DEPLOY_SHA.txt and DEPLOY_SHA from one commit so they cannot disagree.
#
# Deploy (records a commit; overwrites both files):
#   bash write_deploy_sha.sh /opt/trading-agent
#   bash write_deploy_sha.sh /opt/trading-agent 60a1dc10
#   DEPLOY_SHA=60a1dc10 bash write_deploy_sha.sh /opt/trading-agent
#   bash write_deploy_sha.sh /opt/trading-agent "" /path/to/git/checkout
#
# Resolution order when the sha argument is empty:
#   1. DEPLOY_SHA environment variable
#   2. git -C <git dir> rev-parse --short=8 HEAD (third argument, else the dest)
#
# Cycle reconcile (does not advance the stamp to a newer git HEAD):
#   source write_deploy_sha.sh
#   reconcile_deploy_sha /opt/trading-agent
# DEPLOY_SHA.txt wins when the two files differ. A missing partner is copied
# from the one that exists. If neither exists, the files are left absent.

set -euo pipefail

_deploy_sha_valid() {
    [[ "${1:-}" =~ ^[0-9a-fA-F]{7,40}$ ]]
}

_write_both_deploy_sha() {
    local dest="$1"
    local sha="$2"
    if ! _deploy_sha_valid "$sha"; then
        echo "write_deploy_sha: refusing invalid sha '${sha}'" >&2
        return 1
    fi
    mkdir -p "$dest"
    local tmp
    tmp="$(mktemp "${dest}/.DEPLOY_SHA.XXXXXX")"
    printf '%s\n' "$sha" > "$tmp"
    cp "$tmp" "${dest}/DEPLOY_SHA.txt"
    cp "$tmp" "${dest}/DEPLOY_SHA"
    rm -f "$tmp"
    DEPLOY_SHA_VALUE="$sha"
}

_resolve_deploy_sha() {
    local explicit="${1:-}"
    local git_dir="${2:-}"
    if _deploy_sha_valid "$explicit"; then
        printf '%s' "$explicit"
        return 0
    fi
    if _deploy_sha_valid "${DEPLOY_SHA:-}"; then
        printf '%s' "$DEPLOY_SHA"
        return 0
    fi
    if [[ -n "$git_dir" ]] && git -C "$git_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$git_dir" rev-parse --short=8 HEAD
        return 0
    fi
    return 1
}

write_deploy_sha() {
    local dest="${1:-}"
    local explicit="${2:-}"
    local git_dir="${3:-$dest}"
    if [[ -z "$dest" ]]; then
        echo "usage: write_deploy_sha DEST [SHA] [GIT_DIR]" >&2
        return 1
    fi
    local sha
    if ! sha="$(_resolve_deploy_sha "$explicit" "$git_dir")"; then
        echo "write_deploy_sha: no sha (pass SHA, set DEPLOY_SHA, or deploy from a git checkout)" >&2
        return 1
    fi
    _write_both_deploy_sha "$dest" "$sha"
}

_read_sha_file() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    local token
    token="$(awk 'NF { print $1; exit }' "$path")"
    if _deploy_sha_valid "$token"; then
        printf '%s' "$token"
        return 0
    fi
    return 1
}

reconcile_deploy_sha() {
    local dest="${1:-.}"
    local canonical="" mirror=""
    canonical="$(_read_sha_file "${dest}/DEPLOY_SHA.txt" || true)"
    mirror="$(_read_sha_file "${dest}/DEPLOY_SHA" || true)"
    DEPLOY_SHA_VALUE=""
    if [[ -n "$canonical" && -n "$mirror" && "$canonical" == "$mirror" ]]; then
        DEPLOY_SHA_VALUE="$canonical"
        return 0
    fi
    if [[ -n "$canonical" && -n "$mirror" && "$canonical" != "$mirror" ]]; then
        echo "deploy sha files disagree (${canonical} vs ${mirror}); rewriting both from DEPLOY_SHA.txt" >&2
        _write_both_deploy_sha "$dest" "$canonical"
        return 0
    fi
    local sha="${canonical}${mirror}"
    if [[ -z "$sha" ]]; then
        return 0
    fi
    echo "only one deploy sha file was present; writing both as ${sha}" >&2
    _write_both_deploy_sha "$dest" "$sha"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    write_deploy_sha "${1:-}" "${2:-}" "${3:-${1:-}}"
fi
