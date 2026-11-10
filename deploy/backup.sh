#!/usr/bin/env bash
# Tägliche Sicherung der Server-Datenbank wissensraum_server (Paritäts-Roadmap Phase 0, S-03). Läuft auf dem VPS.
#
# 1. notiert den Stand des Audit-Verlaufs (Anzahl:Hash) vor dem Dump,
# 2. erzeugt mit pg_dump im PostgreSQL-Container einen Dump (Format custom) mit der Eigentümerrolle,
# 3. verschlüsselt ihn mit age für den öffentlichen Schlüssel in backup-recipient.txt,
# 4. legt Dump, Audit-Stand und SHA-256 unter WR_BACKUP_DIR ab und löscht dort Dateien älter als
#    WR_BACKUP_KEEP_DAYS. Unraid holt die Dateien mit backup-pull.sh ab.
#
# Unverschlüsselte Daten berühren die Platte nie. Der private age-Schlüssel liegt nicht auf dem VPS.
# Aufruf: WR_PG_CONTAINER=<postgres-container> deploy/backup.sh
set -euo pipefail
umask 077

DEPLOY_DIR=$(cd "$(dirname "$0")" && pwd)
PG_CONTAINER=${WR_PG_CONTAINER:?WR_PG_CONTAINER: Name des PostgreSQL-Containers fehlt}
TARGET=${WR_BACKUP_DIR:-/var/backups/wissensraum}
RECIPIENTS=${WR_BACKUP_RECIPIENTS:-$DEPLOY_DIR/backup-recipient.txt}
KEEP_DAYS=${WR_BACKUP_KEEP_DAYS:-7}
OWNER_SECRET=$DEPLOY_DIR/secrets/db_owner.txt
DATABASE=wissensraum_server
OWNER=wissensraum_owner

command -v age >/dev/null || { echo "age fehlt (dnf install age)." >&2; exit 2; }
[ -s "$RECIPIENTS" ] || { echo "Öffentlicher age-Schlüssel fehlt: $RECIPIENTS" >&2; exit 2; }
[ -s "$OWNER_SECRET" ] || { echo "Kennwort der Eigentümerrolle fehlt: $OWNER_SECRET" >&2; exit 2; }
mkdir -p "$TARGET"

name="$DATABASE-$(date -u +%Y%m%dT%H%M%SZ)"
cleanup() { rm -f "$TARGET/$name".*.tmp; }
trap cleanup EXIT

# 1. Audit-Stand vor dem Dump; die Übung prüft, dass die Kopie genau diesen Stand enthält.
#    --no-deps: der Migrationsdienst läuft dabei nicht mit.
docker compose -f "$DEPLOY_DIR/compose.yaml" run --rm --no-deps -T server python server.py audit-verify \
  | python3 -c 'import json, sys; d = json.load(sys.stdin); d["ok"] or sys.exit("Audit-Kette ungueltig: " + str(d)); print(str(d["entries"]) + ":" + d["head"])' \
  > "$TARGET/$name.audit.tmp"

# 2. und 3. Das Kennwort geht über stdin in den Container, nicht über Argumente oder Umgebung des Hosts.
docker exec -i "$PG_CONTAINER" sh -c \
  "read -r PGPASSWORD; export PGPASSWORD; exec pg_dump -h 127.0.0.1 -U $OWNER -d $DATABASE -Fc" \
  < "$OWNER_SECRET" | age -R "$RECIPIENTS" > "$TARGET/$name.dump.age.tmp"

[ -s "$TARGET/$name.dump.age.tmp" ] || { echo "Leerer Dump." >&2; exit 1; }

# 4. Erst vollständige Dateien bekommen ihren Namen; backup-pull.sh überträgt keine *.tmp.
mv "$TARGET/$name.audit.tmp" "$TARGET/$name.audit"
mv "$TARGET/$name.dump.age.tmp" "$TARGET/$name.dump.age"
(cd "$TARGET" && sha256sum "$name.dump.age" "$name.audit" > "$name.sha256")
find "$TARGET" -maxdepth 1 -type f -name "$DATABASE-*" -mtime +"$KEEP_DAYS" -delete

echo "Sicherung $name: $(stat -c %s "$TARGET/$name.dump.age") Byte, Audit-Stand $(cut -d: -f1 "$TARGET/$name.audit") Einträge."
