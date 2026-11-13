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

Ablauf: Der VPS erzeugt täglich einen mit age verschlüsselten Dump (`deploy/backup.sh`). Dein Windows-Rechner holt ihn über WireGuard ab (`deploy/backup-pull.ps1`). Die Übung läuft auf dem VPS (`deploy/restore-drill.sh`), der private Schlüssel kommt dabei über ssh von deinem Rechner und berührt die Platte des VPS nicht. Der VPS hat keinen Zugang zu deinem Rechner. Unraid ist nicht beteiligt (Entscheidung 12.11.2026). Für einen Linux-Rechner als Abholer bleibt `deploy/backup-pull.sh`.

Einmalig auf deinem Rechner (PowerShell), Ordner `%USERPROFILE%\Wissensraum-Sicherung`:

1. `winget install FiloSottile.age`, dann `age-keygen -o wissensraum-backup.key`. Die Datei zusätzlich in den Passwortmanager und auf ein Offline-Medium. Ohne sie ist keine Sicherung lesbar.
2. `ssh-keygen -t ed25519 -N '""' -f wrbackup_ed25519` für das Abholen.
3. Den öffentlichen age-Schlüssel (Zeile `# public key: age1...`) auf dem VPS in `deploy/backup-recipient.txt` schreiben. Er ist kein Geheimnis.

Einrichtung des VPS ohne Eingaben in dessen Shell, von deinem Rechner aus:

```powershell
Get-Content wrbackup_ed25519.pub | ssh root@10.13.13.1 bash /opt/docker/wissen/app/deploy/backup-setup.sh --run
```

`backup-setup.sh` installiert age und acl, legt den Benutzer `wrbackup` an, gibt ihm über eine Standard-ACL Leserecht auf `/var/backups/wissensraum`, trägt den Schlüssel mit `restrict,from="10.13.13.2"` ein, installiert Service und Timer mit dem erkannten PostgreSQL-Container und legt eine mit age verschlüsselte Kopie von `secrets/mail_key.txt` in den Sicherungsordner. `--run` startet den ersten Lauf und zeigt das Journal. Prüfpunkt: In `/var/backups/wissensraum/` liegen `.dump.age`, `.audit` und `.sha256`. Der Aufruf lässt sich gefahrlos wiederholen. Andere Adresse des Abholers: `WR_PULL_FROM`, anderer Container: `WR_PG_CONTAINER`.

Abholen auf deinem Rechner:

1. Einmal von Hand: `powershell -ExecutionPolicy Bypass -File deploy\backup-pull.ps1`. Beim ersten Mal merkt sich ssh den Host-Schlüssel des VPS. Das Skript prüft alle Prüfsummen und meldet Exit 1, wenn die neueste Sicherung älter als 36 Stunden ist.
2. Täglich einplanen: dasselbe mit `-Register` (Standard 12:00, verpasste Läufe werden nachgeholt).
3. Danach auf dem VPS den Timer einschalten: `Get-Content wrbackup_ed25519.pub | ssh root@10.13.13.1 bash /opt/docker/wissen/app/deploy/backup-setup.sh --timer`.

Übung vierteljährlich, von deinem Rechner aus:

```powershell
Get-Content wissensraum-backup.key | ssh root@10.13.13.1 "WR_AGE_IDENTITY=- WR_PG_IMAGE=pgvector/pgvector:pg18 /opt/docker/wissen/app/deploy/restore-drill.sh"
```

Sie spielt die neueste Sicherung in einen Container ohne Netzwerk ein, zählt die Zeilen und prüft die Audit-Kette gegen den Stand vor dem Dump. `WR_PG_IMAGE` nutzt das vorhandene Datenbank-Image statt `postgres:18` nachzuladen. Ergebnis steht in `/var/backups/wissensraum/restore-drill.log` und kommt mit dem nächsten Abholen auf deinen Rechner.

Nachweis im Repository: Dump mit der Eigentümerrolle, Rückspielen mit `--no-owner --no-privileges` und `server.py audit-verify --expect` auf der Kopie wurden am 10.11.2026 gegen PostgreSQL 16 geprüft, ebenso `backup.sh` mit echtem age und pg_dump und einem Ersatz für docker. Am 12.11.2026: `backup-setup.sh` mit echtem useradd, setfacl und age und Ersatz für dnf, docker und systemctl (Wiederholung, Schlüssel aus stdin mit Windows-Zeilenende, Ablehnung falscher Eingaben), `backup-pull.ps1` unter Windows PowerShell mit Ersatz für ssh und scp (Abholen ohne `.tmp`, falsche Prüfsumme und zu alte Sicherung ergeben Exit 1), age mit Schlüssel aus stdin. Erster echter Lauf am 13.11.2026 auf dem VPS: Sicherung, Abholung und Einspielen bestanden, die Audit-Prüfung brach ab, weil das laufende Image `wissensraum-server:3.0` vor `audit-verify` noch die Anmeldeeinstellungen verlangt und `--expect` nicht kennt. Seitdem gibt die Übung dem Prüf-Container Platzhalter dafür mit (ohne Netzwerk, nie benutzt) und gleicht Anzahl und Kopf der Kette selbst mit dem Stand vor dem Dump ab. Geprüft mit Ersatz für docker in sechs Fällen (gleicher Stand, anderer Kopf, weniger und mehr Einträge, defekte Kette, Abbruch des Images) und gegen `settings_from_env` des Stands `bf3b126`.

## Worker und semantische Suche (Paritäts-Roadmap Phase 2)

Der Dienst `worker` läuft aus demselben Image wie `server` (`python worker.py`), ohne Port und ohne Anmeldedaten. Er bettet Abschnitte für die semantische Suche im Hintergrund ein. `WR_OLLAMA_URL` gilt jetzt auch für Einbettungen, nicht nur für Antworten.

1. Auf dem VPS das Embedding-Modell laden: `docker exec <ollama-container> ollama pull qwen3-embedding:0.6b`. Hängt Ollama wie auf dem VPS nur im internen Netz `backend`, scheitert der Download an der Namensauflösung. Dann kurz verbinden und wieder trennen: `docker network connect bridge ollama`, Pull, `docker network disconnect bridge ollama`. Ollama bekommt dabei keinen veröffentlichten Port (so am 13.11.2026).
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

Outlook.com, Hotmail, Live (D-032): Diese Postfächer liegen nicht im Mandanten, und Outlook.com nimmt seit 16.09.2024 kein IMAP mit Kennwort an. Einschalten mit `WR_MAIL_PERSONAL_ACCOUNTS=1` in `deploy/wissensraum.env`. Die Entra-App muss dafür unter Authentifizierung „Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten“ zulassen (lehnt das Portal ab: im Manifest zuerst `requestedAccessTokenVersion` auf 2) und die Umleitungs-URI `https://wissen.melchy.cloud/postfaecher/microsoft/privat/rueckruf` haben. Die Server-Anmeldung prüft weiter den Mandanten. Danach erscheint unter Postfächer „Privates Microsoft-Postfach verbinden“.

Schlüsselwechsel: neuen Schlüssel als `mail_key`, den alten als zweites Secret und `WR_MAIL_KEY_PREVIOUS_FILE`, Container neu anlegen, `docker compose exec server python server.py mail-rewrap`, danach den alten Schlüssel entfernen.

## Antwortqualität von qwen3:4b messen

Alle Grenzwerte (False Reject höchstens 10 %, False Accept höchstens 5 %) sind mit `qwen3:8b` gemessen. Für das Modell des Servers gilt der Nachweis erst nach eigener Messung auf dem eingefrorenen Bestand V2, auf deinem Rechner, mit exklusiv laufendem Ollama:

```
ollama pull qwen3:4b
python tests/eval/evaluate.py --own data/<eigene-fragen>.json --db data/<messkopie>.db --answers qwen3:4b --json > data/bericht-qwen3-4b.json
```

Liegen False Reject und False Accept innerhalb der Grenzen, bleibt `qwen3:4b`. Sonst auf `qwen3:8b` wechseln (`WR_AI_MODEL`, rund 6 GB Speicher für Ollama). Ergebnis nur mit Frage-IDs in den Masterplan, Messkopie danach löschen.

## Update auf neuen Code (Lehren vom 13.11.2026)

- Vorher frisch sichern (`systemctl start wissensraum-backup.service`) und das laufende Image markieren (`docker tag wissensraum-server:3.0 wissensraum-server:3.0-vor-update`). `up --build` überschreibt sonst das Tag, und der Rückweg geht nur noch über die Image-ID.
- `deploy/wissensraum.env` vor dem Ändern kopieren (`wissensraum.env.vor-update`).
- Befehle per ssh von Windows aus als Skript auf stdin schicken (`… | ssh root@10.13.13.1 "tr -d '\r' | bash -s"`), nicht in die Shell des VPS einfügen. Dort stört Bracketed Paste. `docker compose run` und `docker exec -i` lesen sonst den Rest des Skripts: mit `-T` und `</dev/null` aufrufen.
- Zwischen Build und Migration verlangt `audit-verify` im neuen Image Schema 26. Den Timer der Sicherung deshalb nicht mitten im Update laufen lassen, Build und `up -d` direkt hintereinander.

## Erledigt

- Veröffentlichtes PostgreSQL-Kennwort rotiert (Angabe des Product Owners am 10.11.2026). Datum der Rotation und betroffene Rollen hier nachtragen.
- Erste Sicherung auf dem VPS 13.11.2026 07:44 UTC, Abholung auf den Windows-Rechner, erste Wiederherstellungsübung bestanden 13.11.2026 08:44 UTC. Timer täglich 02:17 UTC, Abholung täglich 12:00.
- Update auf Schema 26 am 13.11.2026 09:04 UTC, Server-Container neu angelegt (alte Logs verworfen), Worker mit OCR, Einbettungsmodell geladen.

## Offen bis zur Abnahme von 10.8

- wöchentlicher `pip-audit` und Prüfung des Basis-Images
- Ablaufdatum des Entra-Client-Geheimnisses in `WR_OIDC_CLIENT_SECRET_EXPIRES` eintragen (Warnung seit Phase 5 eingebaut)
- Entra-Zustimmung für `Mail.Read` und `offline_access`, bei Hotmail zusätzlich die Kontotypen nach D-032
- Probelauf mit zwei echten Konten im Browser
