#!/usr/bin/env bash
# Richtet die tägliche Sicherung auf dem VPS ein (Paritäts-Roadmap Phase 0, S-03). Läuft als root auf dem VPS.
#
# Gedacht für den Aufruf von deinem Rechner über ssh, damit nichts in die Shell des VPS eingefügt werden muss.
# Der öffentliche SSH-Schlüssel des abholenden Rechners kommt über stdin:
#
#   Get-Content wrbackup_ed25519.pub | ssh root@10.13.13.1 bash /opt/docker/wissen/app/deploy/backup-setup.sh
#
# Schritte, jeder lässt sich gefahrlos wiederholen:
#   1. age und acl installieren (age aus EPEL). rsync braucht nur das Linux-Abholskript backup-pull.sh.
#   2. Benutzer wrbackup, Ordner WR_BACKUP_DIR mit Leserecht über eine Standard-ACL,
#   3. authorized_keys von wrbackup: nur der Schlüssel aus stdin, nur von WR_PULL_FROM, mit restrict,
#   4. Service und Timer nach /etc/systemd/system, mit Containername und Pfad dieses Repositorys,
#   5. verschlüsselte Kopie von secrets/mail_key.txt im Sicherungsordner, falls noch keine da ist.
#
# Optionen: --run startet danach den ersten Lauf und zeigt das Journal. --timer schaltet den täglichen Timer ein.
# Der private age-Schlüssel kommt hier nie vor. deploy/backup-recipient.txt muss den öffentlichen enthalten.
set -euo pipefail
umask 022

DEPLOY_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET=${WR_BACKUP_DIR:-/var/backups/wissensraum}
PULL_FROM=${WR_PULL_FROM:-10.13.13.2}
RECIPIENTS=$DEPLOY_DIR/backup-recipient.txt
UNIT_DIR=${WR_UNIT_DIR:-/etc/systemd/system}
HOME_DIR=${WR_BACKUP_HOME:-/home/wrbackup}
run=0
timer=0
for arg in "$@"; do
  case "$arg" in
    --run) run=1 ;;
    --timer) timer=1 ;;
    *) echo "Unbekannte Option: $arg (erlaubt: --run, --timer)" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen." >&2; exit 2; }
grep -q '^age1[0-9a-z]\{58\}$' "$RECIPIENTS" 2>/dev/null \
  || { echo "In $RECIPIENTS fehlt der öffentliche age-Schlüssel (eine Zeile age1...)." >&2; exit 2; }

# Schlüssel aus stdin: genau eine Zeile, Typ und Base64 ohne Optionen. Ohne stdin bleibt authorized_keys unverändert.
key=''
if [ ! -t 0 ]; then
  key=$(tr -d '\r' | grep -v '^[[:space:]]*$' || true)
  if [ -n "$key" ]; then
    [ "$(printf '%s\n' "$key" | wc -l)" -eq 1 ] \
      && printf '%s\n' "$key" | grep -Eq '^(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( [^"]*)?$' \
      || { echo "stdin enthält keinen einzelnen öffentlichen SSH-Schlüssel (ssh-ed25519 ...)." >&2; exit 2; }
  fi
fi
printf '%s\n' "$PULL_FROM" | grep -Eq '^[0-9.]+(/[0-9]+)?$' || { echo "WR_PULL_FROM ist keine IPv4-Adresse." >&2; exit 2; }

container=${WR_PG_CONTAINER:-}
if [ -z "$container" ]; then
  matches=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /(^|\/)(postgres|pgvector)/ {print $1}')
  [ "$(printf '%s\n' "$matches" | grep -c .)" -eq 1 ] \
    || { echo "PostgreSQL-Container nicht eindeutig, bitte WR_PG_CONTAINER setzen. Gefunden: ${matches:-keiner}" >&2; exit 2; }
  container=$matches
fi

echo "== 1. Pakete"
missing=''
for tool in age setfacl python3; do command -v "$tool" >/dev/null || missing="$missing $tool"; done
if [ -n "$missing" ]; then
  rpm -q epel-release >/dev/null 2>&1 || dnf install -y -q epel-release
  dnf install -y -q age acl python3
fi
for tool in age setfacl python3; do command -v "$tool" >/dev/null || { echo "$tool fehlt weiterhin." >&2; exit 1; }; done

echo "== 2. Benutzer und Ordner"
id wrbackup >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash wrbackup
install -d -m 750 -g wrbackup "$TARGET"
setfacl -m g:wrbackup:rx "$TARGET"
setfacl -d -m g:wrbackup:r "$TARGET"

echo "== 3. Zugang für den abholenden Rechner ($PULL_FROM)"
install -d -m 700 -o wrbackup -g wrbackup "$HOME_DIR/.ssh"
if [ -n "$key" ]; then
  printf 'restrict,from="%s" %s\n' "$PULL_FROM" "$key" > "$HOME_DIR/.ssh/authorized_keys"
  chown wrbackup:wrbackup "$HOME_DIR/.ssh/authorized_keys"
  chmod 600 "$HOME_DIR/.ssh/authorized_keys"
else
  echo "Kein Schlüssel über stdin, authorized_keys bleibt wie es ist."
fi
command -v restorecon >/dev/null && restorecon -R "$HOME_DIR/.ssh" "$TARGET" || true

echo "== 4. Service und Timer (Container $container)"
sed -e "s|^Environment=WR_PG_CONTAINER=.*|Environment=WR_PG_CONTAINER=$container|" \
    -e "s|^Environment=WR_BACKUP_DIR=.*|Environment=WR_BACKUP_DIR=$TARGET|" \
    -e "s|^ExecStart=.*|ExecStart=$DEPLOY_DIR/backup.sh|" \
    "$DEPLOY_DIR/wissensraum-backup.service" > "$UNIT_DIR/wissensraum-backup.service"
cp "$DEPLOY_DIR/wissensraum-backup.timer" "$UNIT_DIR/wissensraum-backup.timer"
command -v systemctl >/dev/null && systemctl daemon-reload || true

echo "== 5. Kopie des Postfach-Schlüssels"
if [ -s "$DEPLOY_DIR/secrets/mail_key.txt" ] && [ ! -s "$TARGET/mail_key.txt.age" ]; then
  age -R "$RECIPIENTS" -o "$TARGET/mail_key.txt.age" "$DEPLOY_DIR/secrets/mail_key.txt"
  echo "mail_key.txt.age angelegt."
else
  echo "Nichts zu tun."
fi

if [ "$run" -eq 1 ]; then
  echo "== Erster Lauf"
  status=0
  systemctl start wissensraum-backup.service || status=$?
  journalctl -u wissensraum-backup -n 20 --no-pager || true
  ls -l "$TARGET"
  [ "$status" -eq 0 ] || exit "$status"
fi
if [ "$timer" -eq 1 ]; then
  systemctl enable --now wissensraum-backup.timer
  systemctl list-timers wissensraum-backup.timer --no-pager
fi
echo "Fertig."
