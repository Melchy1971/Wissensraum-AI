#!/usr/bin/env bash
# Holt die verschlüsselten Sicherungen vom VPS auf Unraid (Paritäts-Roadmap Phase 0, S-03). Läuft auf Unraid,
# zum Beispiel täglich über das Plugin "User Scripts", über WireGuard.
#
# Der VPS braucht dafür keinen Zugang zu Unraid. Auf dem VPS gibt es den Benutzer wrbackup, der nur
# WR_BACKUP_DIR lesen darf (Einrichtung in deploy/BETRIEB.md). Übertragen werden nur fertige Dateien,
# danach werden die Prüfsummen auf Unraid geprüft.
set -euo pipefail
umask 077

VPS=${WR_VPS:-wrbackup@10.13.13.1}
REMOTE_DIR=${WR_REMOTE_DIR:-/var/backups/wissensraum/}
DEST=${WR_BACKUP_DEST:-/mnt/user/backups/wissensraum}
SSH_KEY=${WR_SSH_KEY:-/boot/config/ssh/wrbackup_ed25519}
KEEP_DAYS=${WR_BACKUP_KEEP_DAYS:-90}

mkdir -p "$DEST"
rsync -a --ignore-existing --exclude '*.tmp' \
  -e "ssh -i $SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  "$VPS:$REMOTE_DIR" "$DEST/"

cd "$DEST"
failed=0
for sums in *.sha256; do
  [ -e "$sums" ] || continue
  sha256sum --quiet -c "$sums" || { echo "Prüfsumme falsch: $sums" >&2; failed=1; }
done
find "$DEST" -maxdepth 1 -type f -name 'wissensraum_server-*' -mtime +"$KEEP_DAYS" -delete

newest=$(ls -1 wissensraum_server-*.dump.age 2>/dev/null | tail -n 1 || true)
[ -n "$newest" ] || { echo "Keine Sicherung vorhanden." >&2; exit 1; }
age_hours=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
echo "Neueste Sicherung: $newest, vor $age_hours Stunden."
[ "$age_hours" -le 36 ] || { echo "Neueste Sicherung ist älter als 36 Stunden." >&2; failed=1; }
exit "$failed"
