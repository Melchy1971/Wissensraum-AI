#!/usr/bin/env bash
# Wiederherstellungsübung für die Server-Datenbank (Phase 11.0, S-03). Läuft auf Unraid.
#
# Entschlüsselt eine Sicherung, spielt sie in einen Wegwerf-Container postgres:18 ohne Netzwerk ein,
# zählt die Zeilen je Tabelle und prüft die Audit-Kette mit dem Server-Image gegen den Stand, der vor
# dem Dump notiert wurde (server.py audit-verify --expect). Danach wird alles entfernt.
#
# Voraussetzungen: age, docker, der private age-Schlüssel (WR_AGE_IDENTITY) und das Image
# wissensraum-server:3.0 im selben Stand wie auf dem VPS (docker build -t wissensraum-server:3.0 .).
# Aufruf: WR_AGE_IDENTITY=/pfad/zum/schluessel deploy/restore-drill.sh [sicherung.dump.age]
set -euo pipefail
umask 077

DEST=${WR_BACKUP_DEST:-/mnt/user/backups/wissensraum}
IDENTITY=${WR_AGE_IDENTITY:?WR_AGE_IDENTITY: privater age-Schlüssel fehlt}
PG_IMAGE=${WR_PG_IMAGE:-postgres:18}
SERVER_IMAGE=${WR_SERVER_IMAGE:-wissensraum-server:3.0}
LOG=${WR_DRILL_LOG:-$DEST/restore-drill.log}
DATABASE=wissensraum_restore_test

file=${1:-$(ls -1 "$DEST"/wissensraum_server-*.dump.age | tail -n 1)}
base=${file%.dump.age}
container="wr-restore-drill-$$"
work=$(mktemp -d)
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

(cd "$(dirname "$file")" && sha256sum --quiet -c "$(basename "$base").sha256")
expect=$(cat "$base.audit")
age -d -i "$IDENTITY" -o "$work/dump" "$file"

# Zufallskennwort nur für diesen isolierten Container; er hat kein Netzwerk und lebt nur für die Übung.
password=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
docker run -d --name "$container" --network none -e POSTGRES_PASSWORD="$password" "$PG_IMAGE" >/dev/null
for _ in $(seq 60); do
  docker exec "$container" pg_isready -U postgres -q && break
  sleep 1
done
docker exec "$container" createdb -U postgres "$DATABASE"
docker exec -i "$container" pg_restore -U postgres -d "$DATABASE" --no-owner --no-privileges --exit-on-error \
  < "$work/dump"

counts=$(docker exec "$container" psql -U postgres -d "$DATABASE" -At -F ' ' -c "
  SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
         table_schema, table_name), false, true, '')))[1]::text::int
  FROM information_schema.tables WHERE table_schema = 'wissensraum' AND table_type = 'BASE TABLE' ORDER BY 1")

# Das Server-Image teilt sich den Netzwerkraum des Containers: localhost ohne TLS ist dort zulässig.
status=0
verify=$(docker run --rm --network "container:$container" \
  -e DB_HOST=127.0.0.1 -e DB_PORT=5432 -e DB_DATABASE="$DATABASE" -e DB_USERNAME=postgres \
  -e DB_PASSWORD="$password" -e DB_SSLMODE=disable \
  "$SERVER_IMAGE" python server.py audit-verify --expect "$expect") || status=$?

{
  echo "== $(date -Iseconds) Übung mit $(basename "$file")"
  echo "Audit erwartet: $expect"
  echo "Audit geprüft:  $verify"
  echo "Zeilen je Tabelle:"
  echo "$counts"
  [ "$status" -eq 0 ] && echo "Ergebnis: bestanden" || echo "Ergebnis: FEHLGESCHLAGEN"
} | tee -a "$LOG"
exit "$status"
