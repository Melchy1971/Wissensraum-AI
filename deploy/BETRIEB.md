# Betrieb des Servers auf dem VPS (Release 3.0, Schritt 10.8)

Stand 11.11.2026, Entscheidung D-029, Code-Stand `1298469`, Schema 26: Der Server läuft auf dem IONOS-VPS (`85.215.131.200`, Rocky Linux 10, Docker) unter `https://wissen.melchy.cloud` und ist nur aus dem WireGuard-Netz erreichbar. Er nutzt eine eigene Datenbank `wissensraum_server` im vorhandenen PostgreSQL 18 und das vorhandene Ollama. Die Datenbank der Desktop-Anwendung bleibt unberührt.

Für die Inbetriebnahme der Paritäts-Roadmap (Phase 0 bis 9) gibt es eine Schrittliste mit Prüfpunkten: `deploy/VPS-ABLAUF.md`.

## Ablauf

1. **DNS:** A-Eintrag `wissen.melchy.cloud` auf `85.215.131.200`.
2. **WireGuard:** Der VPS bekommt das Netz `10.13.13.0/24` mit Adresse `10.13.13.1` und UDP-Port 51820. Jeder Client bekommt eine eigene Adresse. Im Client steht `AllowedIPs = 10.13.13.0/24, 85.215.131.200/32`. Dann laufen nur Verbindungen zum VPS durch den Tunnel und kommen mit der VPN-Adresse an. Die Firewall des VPS öffnet nur 22, 80, 443 und 51820/udp. PostgreSQL und Ollama bleiben ohne veröffentlichten Port.
3. **Traefik:** Der Router für `wissen.melchy.cloud` holt ein Let's-Encrypt-Zertifikat (`letsencrypt`, Entrypoint `websecure`, Netz `proxy`). Die VPN-Sperre ist die Middleware `wissen-vpn-only` aus der Traefik-Dateikonfiguration des VPS, nicht aus dem Repository. Sie muss nur `10.13.13.0/24` durchlassen, alle anderen bekommen 403. Wer sie ändert, prüft danach ohne VPN (Schritt 15 in `VPS-ABLAUF.md`). PostgreSQL und Ollama liegen im Netz `backend`.
4. **PostgreSQL:** Im Container muss `ssl=on` gesetzt sein, denn die Anwendung verlangt TLS außer bei localhost. Danach `datenbank.sql` einmal als Superuser ausführen. Das legt die Datenbank und die Rollen `wissensraum_owner` (Migration) und `wissensraum_app` (Betrieb) an.
5. **Geheimnisse:** Im Ordner `deploy/secrets/` liegen `db_owner.txt`, `db_app.txt` und `oidc_client_secret.txt`. Die Dateien sind mit `chmod 600` geschützt und gehören weder ins Repository noch in Logs. Die Kennwörter erzeugst du mit `openssl rand -base64 32`.
6. **Entra ID:** In der App-Registrierung steht als Umleitungs-URI `https://wissen.melchy.cloud/auth/callback`. Ein Client-Geheimnis mit Ablaufdatum wird angelegt. Tenant-ID und Client-ID kommen in `wissensraum.env`, siehe `wissensraum.env.beispiel`.
7. **Start:** `docker compose -f deploy/compose.yaml up -d --build`. Der Dienst `migrate` migriert zuerst mit der Eigentümerrolle und setzt die Rechte der Laufzeitrolle. Danach startet `server` mit der Laufzeitrolle und prüft beim Start nur, ob das Schema aktuell ist.
8. **Erster Administrator:** `docker compose -f deploy/compose.yaml run --rm server python server.py account-add --tenant <tid> --object <oid> --role admin --name <Name>`. Tenant- und Objekt-ID des eigenen Entra-Kontos stehen im Entra-Portal unter Benutzer.

## Rechte der Laufzeitrolle

`wissensraum_app` darf Anwendungstabellen lesen und schreiben. In der Migrationshistorie darf sie nur lesen. Im Audit-Verlauf darf sie nur lesen und einfügen, ohne Ändern, Löschen oder TRUNCATE. DDL ist ihr nicht erlaubt. Nachweis: `test_server_runtime_role_on_real_postgresql`. Zusätzlich verweigern Trigger jede Änderung am Audit-Verlauf, auch für den Eigentümer.

## Prüfung nach dem Start

- `docker compose -f deploy/compose.yaml ps` zeigt `server` als healthy.
- Ohne VPN antwortet `https://wissen.melchy.cloud` mit 403, mit VPN erscheint die Anmeldeseite.
- `docker compose -f deploy/compose.yaml run --rm server python server.py audit-verify` prüft die Hash-Kette des Audit-Verlaufs. Anzahl und letzten Hash aus der Ausgabe notieren.

## Protokoll ohne Anmeldecode und Suchbegriffe (Paritäts-Roadmap Phase 0, S-01)

Der Server schreibt im Zugriffsprotokoll nur noch den Pfad, keine Query. Vorher standen `code` und `state` der Anmeldung und die Suchbegriffe aus `/suche?q=` im Docker-Log. Nach dem Update einmal die alten Logs verwerfen: `docker compose -f deploy/compose.yaml up -d --force-recreate server` legt den Container neu an, das alte `json-file`-Log wird dabei entfernt. Prüfen: `docker compose -f deploy/compose.yaml logs server | grep -c 'code='` ergibt 0. Falls Traefik ein Accesslog schreibt, dort ebenfalls Querystrings prüfen (`accessLog.fields.names.RequestPath` bzw. `RequestQuery` auf `drop`).

## Sicherung und Wiederherstellungsübung (Paritäts-Roadmap Phase 0, S-03)

Ablauf: Der VPS erzeugt täglich einen mit age verschlüsselten Dump (`deploy/backup.sh`). Unraid holt ihn über WireGuard ab (`deploy/backup-pull.sh`). Die Übung läuft auf Unraid (`deploy/restore-drill.sh`). Der private age-Schlüssel liegt nur bei dir, nie auf dem VPS. Der VPS hat keinen Zugang zu Unraid.

Einmalige Einrichtung auf deinem Rechner oder Unraid:

1. `age-keygen -o wissensraum-backup.key`. Die Datei sicher ablegen (Passwortmanager plus Offline-Kopie). Ohne sie ist keine Sicherung lesbar.
2. Die Zeile `Public key: age1...` notieren.

Auf dem VPS:

1. `dnf install age rsync` (age liegt in EPEL).
2. Den öffentlichen Schlüssel (nur `age1...`) in `deploy/backup-recipient.txt` schreiben. Er ist kein Geheimnis.
3. Benutzer und Ordner: `useradd --system --create-home --shell /bin/bash wrbackup`, dann `install -d -m 750 -g wrbackup /var/backups/wissensraum` und `setfacl -m g:wrbackup:rx /var/backups/wissensraum && setfacl -d -m g:wrbackup:r /var/backups/wissensraum`. Die Standard-ACL gibt jeder neuen Sicherung Leserecht für `wrbackup`, alle anderen Benutzer sehen nichts.
4. In `~wrbackup/.ssh/authorized_keys` den öffentlichen SSH-Schlüssel von Unraid mit Einschränkung eintragen: `restrict,from="10.13.13.0/24" ssh-ed25519 AAAA... unraid`. Wenn `rrsync` vorhanden ist, zusätzlich `command="rrsync -ro /var/backups/wissensraum"` und auf Unraid `WR_REMOTE_DIR=` (leer) setzen.
5. `deploy/wissensraum-backup.service` und `.timer` nach `/etc/systemd/system/` kopieren, Containername eintragen, `systemctl daemon-reload && systemctl enable --now wissensraum-backup.timer`.
6. Erster Lauf von Hand: `systemctl start wissensraum-backup.service && journalctl -u wissensraum-backup -n 20`.

Auf Unraid:

1. SSH-Schlüssel erzeugen: `ssh-keygen -t ed25519 -N '' -f /boot/config/ssh/wrbackup_ed25519`, einmal `ssh -i ... wrbackup@10.13.13.1` zum Bestätigen des Host-Schlüssels.
2. `deploy/backup-pull.sh` im Plugin User Scripts täglich nach dem VPS-Lauf ausführen, zum Beispiel 04:00. Das Skript meldet einen Fehler, wenn die neueste Sicherung älter als 36 Stunden ist.
3. Für die Übung das Server-Image im gleichen Stand bauen: im Repository auf Unraid `docker build -t wissensraum-server:3.0 .`.
4. Übung vierteljährlich: `WR_AGE_IDENTITY=/pfad/wissensraum-backup.key deploy/restore-drill.sh`. Sie spielt die neueste Sicherung in einen Container ohne Netzwerk ein, zählt die Zeilen und prüft die Audit-Kette gegen den Stand vor dem Dump. Ergebnis steht in `restore-drill.log`. Den Schlüssel danach wieder aus dem Arbeitsordner entfernen.

Nachweis im Repository: Dump mit der Eigentümerrolle, Rückspielen mit `--no-owner --no-privileges` und `server.py audit-verify --expect` auf der Kopie wurden am 10.11.2026 gegen PostgreSQL 16 geprüft, ebenso `backup.sh` mit echtem age und pg_dump und einem Ersatz für docker. `restore-drill.sh` lief noch nicht mit echtem Docker. Der erste Lauf auf Unraid ist die Abnahme.

## Worker und semantische Suche (Paritäts-Roadmap Phase 2)

Der Dienst `worker` läuft aus demselben Image wie `server` (`python worker.py`), ohne Port und ohne Anmeldedaten. Er bettet Abschnitte für die semantische Suche im Hintergrund ein. `WR_OLLAMA_URL` gilt jetzt auch für Einbettungen, nicht nur für Antworten.

1. Auf dem VPS das Embedding-Modell laden: `docker exec <ollama-container> ollama pull qwen3-embedding:0.6b`.
2. Prüfen, in welchem Docker-Netz Ollama hängt. Der Worker ist nur im Netz `datenbank`. Liegt Ollama woanders, dieses Netz beim Dienst `worker` in `compose.yaml` ergänzen, nicht das Proxy-Netz.
3. `docker compose -f deploy/compose.yaml up -d --build`, danach zeigt `docker compose -f deploy/compose.yaml ps` beide Dienste als healthy. Der Worker meldet sich alle 30 Sekunden (`WR_WORKER_INTERVAL`).
4. Als Administrator unter Verwaltung, Semantische Suche, „Einschalten“. Der Server prüft einmal, ob Ollama das Modell liefert. Danach steigt der Zähler „Abschnitte mit Vektor“ mit jeder Runde um bis zu 256.
5. Die Suche zeigt ihren Modus über der Trefferliste: „Hybride Suche“, sobald alle Abschnitte einen Vektor haben, vorher „Hybride Suche – Vektorbestand unvollständig“.

Genau ein Worker. Zwei würden dieselben Abschnitte doppelt einbetten (harmlos, aber unnötige Last).

## Uploads und Importaufträge (Paritäts-Roadmap Phase 3)

Hochladen legt einen Auftrag an, der Worker importiert ihn. Mehrere Dateien und ZIP-Archive sind möglich, bis 50 MB je Datei und 100 MB je Upload. Die Dateien liegen bis zum Import in PostgreSQL (`import_items.upload`) und sind damit in der Sicherung. Nach dem Import werden sie dort entfernt, das Original liegt dann am Dokument. Ein gesperrtes Konto oder eines ohne Importrecht stoppt seinen Auftrag. Ein Neustart des Workers setzt laufende Aufträge fort.

Traefik begrenzt den Anfragekörper auf 110 MB (Middleware `wissensraum-groesse` in `compose.yaml`). Nach dem Update prüfen: Ein Upload über 110 MB endet mit 413 von Traefik, bevor der Server ihn liest.

## Verwaltung, Datenschutz und Ablaufwarnung (Paritäts-Roadmap Phase 5)

1. In `deploy/wissensraum.env` setzen: `WR_EXPORT_DIR=/data/exports` und `WR_OIDC_CLIENT_SECRET_EXPIRES` mit dem Ablaufdatum aus Entra ID (App-Registrierung, Zertifikate und Geheimnisse). Ohne Datum zeigt die Betriebsseite einen Hinweis.
2. Image neu bauen, weil es jetzt den Ordner `/data/exports` anlegt: `docker compose -f deploy/compose.yaml up -d --build`. Server und Worker teilen das Volume `exports`.
3. Unter Verwaltung, Betrieb einmal „Schema, Prüfsummen und Audit-Kette prüfen“. Erwartet: keine Probleme.
4. Ab 30 Tagen vor Ablauf steht eine Warnung im Log (`client_secret`) und auf der Verwaltungsseite. Neues Geheimnis in Entra anlegen, `secrets/oidc_client_secret.txt` ersetzen, Datum anpassen, Server neu anlegen.

Der Installationsexport enthält alle Tabellen samt Originalen, also alles, was auch `pg_dump` enthält. Er liegt unverschlüsselt im Volume, bis ein Admin ihn löscht oder der Worker ihn nach einem Tag entfernt. Nur bei Bedarf anfordern. Selektive Löschung entfernt Daten nicht aus bestehenden Sicherungen.

## Texterkennung (Paritäts-Roadmap Phase 6)

Das Image enthält jetzt Tesseract mit Deutsch und Englisch. Nur der Worker erkennt Text, der Server hat `WISSENSRAUM_OCR=0` in `compose.yaml`.

1. Image neu bauen: `docker compose -f deploy/compose.yaml up -d --build`. Danach steht im Worker-Log `worker_started` mit `status` `ocr`. Steht dort `ohne_ocr`, fehlt Tesseract im Image.
2. Probe: ein Foto einer Textseite hochladen. Nach einer Runde ist es importiert und über die Suche auffindbar.
3. Der Worker hat jetzt 1,5 GB Speichergrenze. Ein Scan mit vielen Seiten belegt ihn mehrere Minuten, Einbettung und Export warten so lange.

Dateien ohne erkannten Text bleiben 30 Tage in der Datenbank (`import_items.upload`), damit Besitzer oder Admin sie unter Importe erneut verarbeiten können. Danach entfernt der Worker die Bytes.

## Postfächer (Paritäts-Roadmap Phase 8, D-030)

1. Schlüssel erzeugen: `openssl rand -base64 32 > deploy/secrets/mail_key.txt`, Rechte 600. Eine Kopie mit age verschlüsseln und außerhalb des VPS ablegen, wie die Sicherung. Ohne diesen Schlüssel sind gespeicherte Zugänge nach einer Wiederherstellung unbrauchbar.
2. In `deploy/wissensraum.env`: `WR_MAIL_KEY_FILE=/run/secrets/mail_key`. Server und Worker bekommen das Secret über `compose.yaml`.
3. Entra-App der Server-Anmeldung: unter Authentifizierung die Umleitungs-URI `https://wissen.melchy.cloud/postfaecher/microsoft/rueckruf` ergänzen, unter API-Berechtigungen Microsoft Graph, delegiert, `Mail.Read` und `offline_access` hinzufügen und die Administratorzustimmung für den Mandanten erteilen.
4. Migration 26 läuft mit dem Dienst `migrate`, danach Container neu anlegen.
5. Probe: als Bearbeiter unter Postfächer das eigene Microsoft-Postfach verbinden, Vorschau öffnen, eine Nachricht importieren, nach einer Worker-Runde ist sie ein Dokument des Kontos.

Schlüsselwechsel: neuen Schlüssel als `mail_key`, den alten als zweites Secret und `WR_MAIL_KEY_PREVIOUS_FILE`, Container neu anlegen, `docker compose exec server python server.py mail-rewrap`, danach den alten Schlüssel entfernen.

## Antwortqualität von qwen3:4b messen

Alle Grenzwerte (False Reject höchstens 10 %, False Accept höchstens 5 %) sind mit `qwen3:8b` gemessen. Für das Modell des Servers gilt der Nachweis erst nach eigener Messung auf dem eingefrorenen Bestand V2, auf deinem Rechner, mit exklusiv laufendem Ollama:

```
ollama pull qwen3:4b
python tests/eval/evaluate.py --own data/<eigene-fragen>.json --db data/<messkopie>.db --answers qwen3:4b --json > data/bericht-qwen3-4b.json
```

Liegen False Reject und False Accept innerhalb der Grenzen, bleibt `qwen3:4b`. Sonst auf `qwen3:8b` wechseln (`WR_AI_MODEL`, rund 6 GB Speicher für Ollama). Ergebnis nur mit Frage-IDs in den Masterplan, Messkopie danach löschen.

## Erledigt

- Veröffentlichtes PostgreSQL-Kennwort rotiert (Angabe des Product Owners am 10.11.2026). Datum der Rotation und betroffene Rollen hier nachtragen.

## Offen bis zur Abnahme von 10.8

- erster Lauf von Sicherung, Abholung und Wiederherstellungsübung auf VPS und Unraid
- wöchentlicher `pip-audit` und Prüfung des Basis-Images
- Ablaufdatum des Entra-Client-Geheimnisses in `WR_OIDC_CLIENT_SECRET_EXPIRES` eintragen (Warnung seit Phase 5 eingebaut)
- Probelauf mit zwei echten Konten im Browser
