#!/bin/sh
set -u

arm_token=I_AM_WATCHING
service_name=NIHardwareConnectionService
service_app='/Library/Application Support/Native Instruments/Hardware/Hardware Connection Service/NIHardwareConnectionService.app'
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
probe="$repo_root/build/probes/mk3_raw_display_probe"

if [ "$#" -ne 1 ] || [ "$1" != "$arm_token" ]; then
    printf '%s\n' "Refusing: exact operator-ready token required: $0 $arm_token" >&2
    exit 2
fi
if [ ! -x "$probe" ]; then
    printf '%s\n' "Refusing: probe is not built at $probe" >&2
    exit 2
fi
if [ ! -x "$service_app/Contents/MacOS/$service_name" ]; then
    printf '%s\n' "Refusing: expected service executable is missing." >&2
    exit 2
fi
if ! /usr/bin/codesign --verify "$probe"; then
    printf '%s\n' "Refusing: probe signature verification failed." >&2
    exit 2
fi
if ! /usr/bin/killall -0 "$service_name" 2>/dev/null; then
    printf '%s\n' "Refusing: $service_name was not running, so its prior state cannot be restored." >&2
    exit 2
fi

restored=0
restore_service()
{
    if [ "$restored" -eq 1 ]; then
        return 0
    fi
    restored=1
    if /usr/bin/killall -0 "$service_name" 2>/dev/null; then
        printf '%s\n' "$service_name is already running."
        return 0
    fi
    if ! /usr/bin/open -gj "$service_app"; then
        printf '%s\n' "RECOVERY REQUIRED: launch $service_app manually." >&2
        return 1
    fi
    attempt=0
    while ! /usr/bin/killall -0 "$service_name" 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 50 ]; then
            printf '%s\n' "RECOVERY REQUIRED: $service_name did not restart within five seconds." >&2
            return 1
        fi
        /bin/sleep 0.1
    done
    printf '%s\n' "$service_name restarted."
    return 0
}

on_signal()
{
    trap - HUP INT TERM EXIT
    restore_service
    exit 130
}

trap on_signal HUP INT TERM
trap restore_service EXIT

printf '%s\n' "Stopping exactly $service_name; automatic restart is armed."
if ! /usr/bin/killall -TERM "$service_name"; then
    printf '%s\n' "Service termination failed. Probe was not run." >&2
    exit 1
fi

attempt=0
while /usr/bin/killall -0 "$service_name" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        printf '%s\n' "Service did not stop within five seconds. Probe was not run." >&2
        exit 1
    fi
    /bin/sleep 0.1
done

probe_status=0
"$probe" --write-one-rectangle "$arm_token" || probe_status=$?

trap - EXIT
if ! restore_service; then
    exit 3
fi
exit "$probe_status"
