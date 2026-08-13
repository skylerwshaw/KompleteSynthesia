#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' "usage: $0 /private/tmp/new-capture.pcapng" >&2
    exit 2
fi

capture=$1
case "$capture" in
    /private/tmp/*.pcapng) ;;
    *) printf '%s\n' "refusing: capture must be a new .pcapng under /private/tmp" >&2; exit 2 ;;
esac
if [ -e "$capture" ] || [ -e "$capture.errors.log" ]; then
    printf '%s\n' "refusing: capture or error log already exists" >&2
    exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
callback="$repo_root/scripts/lldb_mk3_usb_capture.py"
service_name=NIHardwareConnectionService
pid=$(/usr/bin/pgrep -x "$service_name" | /usr/bin/head -n 1)
if [ -z "$pid" ]; then
    printf '%s\n' "$service_name is not running" >&2
    exit 2
fi

printf '%s\n' "Attaching read-only bulk-write capture to $service_name pid $pid"
printf '%s\n' "Capture: $capture"
KOMPLETE_SYNTHESIA_MK3_USB_CAPTURE=$capture exec /usr/bin/lldb --no-lldbinit -p "$pid" \
    -o "command script import $callback" \
    -o "breakpoint set -r 'bulk_write_helper::write_to_bulkpipe'" \
    -o "breakpoint command add -F lldb_mk3_usb_capture.capture_write" \
    -o continue
