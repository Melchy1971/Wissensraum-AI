# Betrieb des Servers auf dem VPS (Release 3.0, Schritt 10.8)

Stand 09.11.2026, Entscheidung D-029: Der Server läuft auf dem IONOS-VPS (`85.215.131.200`, Rocky Linux 9, Docker) unter `https://wissen.melchy.cloud` und ist nur aus dem WireGuard-Netz erreichbar. Er nutzt eine eigene Datenbank `wissensraum_server` im vorhandenen PostgreSQL 18 und das vorhandene Ollama. Die Datenbank der Desktop-Anwendung bleibt unberührt.

## Ablauf

1. **DNS:** A-Eintrag `wissen.melchy.cloud` auf `85.215.131.200`.
2. **WireGuard:** Der VPS bekommt das Netz `10.13.13.0/24` mit Adresse `10.13.13.1` und UDP-Port 51820. Jeder Client bekommt eine eigene Adresse. Im Client steht `AllowedIPs = 10.13.13.0/24, 85.215.131.200/32`. Dann laufen nur Verbindungen zum VPS durch den Tunnel und kommen mit der VPN-Adresse an. Die Firewall des VPS öffnet nur 22, 80, 443 und 51820/udp. PostgreSQL und Ollama bleiben ohne veröffentlichten Port.
3. **Traefik:** Der Router für `wissen.melchy.cloud` holt ein Let's-Encrypt-Zertifikat. Die Middleware `ipallowlist` lässt nur `10.13.13.0/24` durch, alle anderen bekommen 403. Die Platzhalter in `compose.yaml` richten sich nach der vorhandenen Traefik-Einrichtung.
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

## Offen bis zur Abnahme von 10.8

- tägliche Sicherung der Server-Datenbank mit `pg_dump` außerhalb des VPS und eine Wiederherstellungsübung (Betriebsprozess in `docs/sicherheit-3.0.md`)
- wöchentlicher `pip-audit` und Prüfung des Basis-Images
- Ablaufwarnung für das Entra-Client-Geheimnis
- Probelauf mit zwei echten Konten im Browser
