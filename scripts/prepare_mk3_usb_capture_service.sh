#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_app='/Library/Application Support/Native Instruments/Hardware/Hardware Connection Service/NIHardwareConnectionService.app'
target_dir="$repo_root/build/debug-hcs"
target_app="$target_dir/NIHardwareConnectionService.app"

if [ ! -x "$source_app/Contents/MacOS/NIHardwareConnectionService" ]; then
    printf '%s\n' "refusing: installed Hardware Connection Service is missing" >&2
    exit 2
fi
if [ -e "$target_app" ]; then
    printf '%s\n' "refusing: prepared service copy already exists at $target_app" >&2
    exit 2
fi

mkdir -p "$target_dir"
/usr/bin/ditto "$source_app" "$target_app"
/usr/bin/codesign --force --deep --sign - \
    --preserve-metadata=identifier,entitlements \
    "$target_app"
/usr/bin/codesign --verify --deep --strict "$target_app"

printf '%s\n' "$target_app"
