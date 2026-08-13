#!/bin/sh
set -u

arm_token=I_AM_WATCHING
service_name=NIHardwareConnectionService
original_app='/Library/Application Support/Native Instruments/Hardware/Hardware Connection Service/NIHardwareConnectionService.app'
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
debug_app="$repo_root/build/debug-hcs/NIHardwareConnectionService.app"
debug_exe="$debug_app/Contents/MacOS/$service_name"
callback="$repo_root/scripts/lldb_mk3_usb_capture.py"
schema="$repo_root/scripts/fixtures/odr_display_schema_2_2_0.json"
python=$(command -v python3)

if { [ "$#" -ne 2 ] && [ "$#" -ne 3 ]; } || [ "$2" != "$arm_token" ]; then
    printf '%s\n' "usage: $0 /private/tmp/new-capture.pcapng $arm_token [cached|upload]" >&2
    exit 2
fi
capture=$1
action=${3-cached}
case "$action" in
    cached|upload) ;;
    *) printf '%s\n' "refusing: action must be cached or upload" >&2; exit 2 ;;
esac
case "$capture" in
    /private/tmp/*.pcapng) ;;
    *) printf '%s\n' "refusing: capture must be a new .pcapng under /private/tmp" >&2; exit 2 ;;
esac
for path in "$capture" "$capture.errors.log" "$capture.lldb.log"; do
    if [ -e "$path" ]; then
        printf '%s\n' "refusing: output already exists: $path" >&2
        exit 2
    fi
done
if [ ! -x "$debug_exe" ]; then
    printf '%s\n' "refusing: prepared service copy is missing; run prepare_mk3_usb_capture_service.sh" >&2
    exit 2
fi
if ! /usr/bin/codesign --verify --deep --strict "$debug_app"; then
    printf '%s\n' "refusing: prepared service signature verification failed" >&2
    exit 2
fi
if ! /usr/bin/killall -0 "$service_name" 2>/dev/null; then
    printf '%s\n' "refusing: original service was not initially running" >&2
    exit 2
fi

restored=0
lldb_pid=

restore_original()
{
    if [ "$restored" -eq 1 ]; then
        return 0
    fi
    restored=1

    if /usr/bin/killall -0 "$service_name" 2>/dev/null; then
        /usr/bin/killall -TERM "$service_name" 2>/dev/null || true
        attempt=0
        while /usr/bin/killall -0 "$service_name" 2>/dev/null; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 50 ]; then
                printf '%s\n' "RECOVERY REQUIRED: temporary service did not terminate." >&2
                return 1
            fi
            /bin/sleep 0.1
        done
    fi
    if [ -n "$lldb_pid" ]; then
        wait "$lldb_pid" 2>/dev/null || true
    fi
    if ! /usr/bin/open -gj "$original_app"; then
        printf '%s\n' "RECOVERY REQUIRED: launch $original_app manually." >&2
        return 1
    fi
    attempt=0
    while ! /usr/bin/killall -0 "$service_name" 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 50 ]; then
            printf '%s\n' "RECOVERY REQUIRED: original service did not restart within five seconds." >&2
            return 1
        fi
        /bin/sleep 0.1
    done
    printf '%s\n' "Original $service_name restarted."
}

on_signal()
{
    trap - HUP INT TERM EXIT
    restore_original
    exit 130
}

trap on_signal HUP INT TERM
trap restore_original EXIT

printf '%s\n' "Stopping original $service_name; automatic restoration is armed."
if ! /usr/bin/killall -TERM "$service_name"; then
    printf '%s\n' "Original service termination failed. Nothing else was launched." >&2
    exit 1
fi
attempt=0
while /usr/bin/killall -0 "$service_name" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        printf '%s\n' "Original service did not stop within five seconds." >&2
        exit 1
    fi
    /bin/sleep 0.1
done

printf '%s\n' "Launching the temporary ad-hoc-signed copy under LLDB."
KOMPLETE_SYNTHESIA_MK3_USB_CAPTURE=$capture /usr/bin/lldb --no-lldbinit --batch "$debug_exe" \
    -o "command script import $callback" \
    -o "breakpoint set -r 'bulk_write_helper::write_to_bulkpipe'" \
    -o "breakpoint command add -F lldb_mk3_usb_capture.capture_write" \
    -o run \
    -o quit >"$capture.lldb.log" 2>&1 &
lldb_pid=$!

attempt=0
while ! /usr/bin/killall -0 "$service_name" 2>/dev/null; do
    if ! /bin/kill -0 "$lldb_pid" 2>/dev/null; then
        printf '%s\n' "Temporary service exited during launch; see $capture.lldb.log" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 100 ]; then
        printf '%s\n' "Temporary service did not launch within ten seconds." >&2
        exit 1
    fi
    /bin/sleep 0.1
done

attempt=0
while [ ! -S '/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks' ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 100 ]; then
        printf '%s\n' "Temporary service did not create its ODR socket within ten seconds." >&2
        exit 1
    fi
    /bin/sleep 0.1
done

printf '%s\n' "Capture armed; waiting read-only for the keyboard to finish registering."
probe_status=0
"$python" "$repo_root/scripts/odr_display_probe.py" wait-device --timeout 15 || probe_status=$?
if [ "$probe_status" -eq 0 ]; then
    if [ "$action" = upload ]; then
        printf '%s\n' "Keyboard registered; uploading and selecting one new bounded asset."
        "$python" "$repo_root/scripts/odr_display_probe.py" trace-upload \
            --schema "$schema" --nonce usb-upload-20260812-a --hold 3 --confirmed || probe_status=$?
    else
        printf '%s\n' "Keyboard registered; selecting one cached checkerboard."
        "$python" "$repo_root/scripts/odr_display_probe.py" trace-static \
            --schema "$schema" --hold 3 --confirmed || probe_status=$?
    fi
fi

trap - EXIT
if ! restore_original; then
    exit 3
fi
exit "$probe_status"
