#!/usr/bin/env bash
# Back up Foundry's DURABLE STATE so a dead SD card means restore-and-resume,
# not start-over. Because a build is resumable, a recent copy of the state dir
# is all you need to continue after a card failure.
#
# Set the destination (another disk, a NAS/SMB mount, a USB stick…):
#   FOUNDRY_BACKUP_DST=/mnt/backup/foundry deploy/pi/backup.sh
# Or rely on the env file (foundry.env) for FOUNDRY_BACKUP_SRC/DST.
set -euo pipefail

SRC="${FOUNDRY_BACKUP_SRC:-/var/lib/foundry}"
: "${FOUNDRY_BACKUP_DST:?set FOUNDRY_BACKUP_DST to a backup location (e.g. /mnt/backup/foundry)}"

mkdir -p "$FOUNDRY_BACKUP_DST"
# -a preserve, --delete mirror, --inplace avoids doubling space for big DB files.
exec rsync -a --delete --inplace "$SRC/" "$FOUNDRY_BACKUP_DST/"
