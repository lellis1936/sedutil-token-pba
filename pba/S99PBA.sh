#!/bin/sh
clear

echo "sedutil token PBA"
echo "Searching for USB unlock token; keyboard fallback after ~5 seconds."

MACHINE_SHARE=/etc/sedutil/machine-share.bin
TOKEN_MOUNT=/mnt/sedtoken
LINUXPBA=/sbin/linuxpba
MAX_SCANS=5
SLEEP_SECS=1

try_token() {
    token_file="$1"

    if /sbin/sedtoken --run-linuxpba \
        "$MACHINE_SHARE" \
        "$token_file" \
        "$LINUXPBA" 2>/tmp/pbaerror.log; then

        return 0
    fi

    return 1
}

try_mount_and_token() {
    dev="$1"

    if /bin/mount -t vfat -o ro "/dev/$dev" "$TOKEN_MOUNT" 2>/dev/null; then
        if [ -f "$TOKEN_MOUNT/SEDUTIL/UNLOCK.BIN" ]; then
            echo "USB unlock token accepted on /dev/$dev. Unlocking..."
            try_token "$TOKEN_MOUNT/SEDUTIL/UNLOCK.BIN"
            result=$?
            /bin/umount "$TOKEN_MOUNT" 2>/dev/null
            [ $result -eq 0 ] && return 0
        else
            /bin/umount "$TOKEN_MOUNT" 2>/dev/null
        fi
    fi

    return 1
}

scan_once() {
    /sbin/mdev -s 2>/dev/null

    for diskpath in /sys/block/sd*; do
        [ -e "$diskpath" ] || continue
        disk="${diskpath##*/}"

        # Prefer partitions first. This supports normal FAT32-token USB sticks.
        for partpath in "$diskpath"/"$disk"[0-9]*; do
            [ -e "$partpath" ] || continue
            part="${partpath##*/}"
            try_mount_and_token "$part" && return 0
        done

        # Then try the whole disk. This supports superfloppy-style FAT tokens.
        try_mount_and_token "$disk" && return 0
    done

    return 1
}

if [ -f "$MACHINE_SHARE" ]; then
    mkdir -p "$TOKEN_MOUNT"

    scan=1
    while [ "$scan" -le "$MAX_SCANS" ]; do
        echo "Token scan $scan/$MAX_SCANS..."
        scan_once && exit 0
        [ "$scan" -eq "$MAX_SCANS" ] && break
        scan=$((scan + 1))
        /bin/sleep "$SLEEP_SECS"
    done

    echo "No usable token found. Falling back to keyboard entry."
fi

exec /sbin/linuxpba 2>/tmp/pbaerror.log
