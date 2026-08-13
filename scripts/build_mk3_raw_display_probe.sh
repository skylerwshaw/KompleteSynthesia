#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir="$repo_root/build/probes"
output="$output_dir/mk3_raw_display_probe"

mkdir -p "$output_dir"
xcrun clang \
    -x objective-c \
    -std=c11 \
    -fobjc-arc \
    -Wall \
    -Wextra \
    -Werror \
    -framework Foundation \
    -framework CoreFoundation \
    -framework IOKit \
    -framework IOUSBHost \
    "$repo_root/scripts/mk3_raw_display_probe.c" \
    -o "$output"
codesign --force --sign - \
    --entitlements "$repo_root/scripts/mk3_raw_display_probe.entitlements" \
    "$output"

printf '%s\n' "$output"
