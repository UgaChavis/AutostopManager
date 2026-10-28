#!/bin/sh
set -eu

# Version checks must remain cheap while another render owns the bounded slot.
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
    cache_dir=/tmp/autostop-j1-chromium-version
    cache_file="$cache_dir/version"
    if [ -f "$cache_file" ] && [ ! -L "$cache_file" ]; then
        IFS= read -r version < "$cache_file"
        case "$version" in
            Chromium\ *) printf '%s\n' "$version"; exit 0 ;;
        esac
    fi
    # This tmpfs is recreated with the container; its Chromium rootfs is
    # immutable. Cache only an actual successful version read, atomically.
    umask 077
    if ! /bin/mkdir -m 0700 "$cache_dir" 2>/dev/null; then
        [ -d "$cache_dir" ] && [ ! -L "$cache_dir" ] || exit 1
    fi
    version="$(/usr/lib/chromium/chromium --version)"
    case "$version" in Chromium\ *) ;; *) exit 1 ;; esac
    temporary="$cache_dir/.version.$$"
    (set -C; printf '%s\n' "$version" > "$temporary")
    /bin/mv -fT "$temporary" "$cache_file"
    printf '%s\n' "$version"
    exit 0
fi

# Chromium156 otherwise exceeds the native128PID boundary. The browser
# remains in the same unprivileged, network-isolated, read-only container.
if [ "$#" -eq 0 ] || [ "$1" != "--autostop-lock-held" ]; then
    exec /usr/bin/flock --no-fork --exclusive --wait 15 /tmp/autostop-j1-chromium.lock \
        "$0" --autostop-lock-held "$@"
fi
shift

if /usr/bin/chromium \
    --renderer-process-limit=1 \
    --num-raster-threads=1 \
    --disable-software-rasterizer \
    --no-zygote \
    "$@"; then
    result=0
else
    result=$?
fi
# Give Chromium's already-exiting auxiliary children a bounded drain before
# the next request can acquire the shared resource slot.
/bin/sleep 0.2
exit "$result"
