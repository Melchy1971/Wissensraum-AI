# Ablauf für den VPS-Tag: Paritäts-Roadmap Phase 0 bis 9 in Betrieb nehmen

Stand 13.11.2026:

| Block | Stand |
|---|---|
| A | 1 bis 3 erledigt (Gate 7 von 7 an `8483177`, `mail_key` angelegt), 4 Entra offen |
| B | erledigt 13.11.2026, Übung bestanden |
| C | erledigt 13.11.2026 09:04 UTC, Schema 26, Ablaufdatum des Client-Geheimnisses fehlt noch |
| D | offen, 18 nach D-032 für Hotmail |
| E, F | offen |

Diese Liste fasst die Schritte aus `deploy/BETRIEB.md` (Phasen 0 bis 8) in eine Reihenfolge. Die Einzelheiten stehen dort. Jeder Schritt hat einen Prüfpunkt. Erst weiter, wenn er stimmt. Schlägt ein Prüfpunkt fehl, steht darunter, was zu tun ist.

Grundregel: erst sichern, dann ändern. Die Migrationen 24, 25 und 26 laufen beim Start. Vorher muss eine geprüfte Sicherung der Server-Datenbank auf deinem Rechner liegen.

## A. Vorbereitung am eigenen Rechner (vor dem VPS)

1. **Aktuellen Hauptbranch übernehmen.** `git fetch origin && git switch main && git pull --ff-only origin main`. Erwarteter Mindeststand dieser Anleitung ist Commit `1298469`, Schema 26.
   Prüfpunkt: `git merge-base --is-ancestor 1298469 HEAD` endet mit Code 0 und `git status` ist sauber.
   Wenn der Fast-forward nicht möglich ist: lokale Änderungen sichern und den abweichenden Stand klären; nicht von Hand mischen.
2. **Matrix-Gate.** Windows: `python tests/local_ci.py run`. Linux: `python tests/local_ci.py run --postgres --pg-bin <PostgreSQL-18-bin>`. Danach `python tests/local_ci.py report`.
   Prüfpunkt: `local-ci-gate-<commit>.md` meldet 7 von 7 grün. Bekannt: der GUI-Pixeltest `page='mail'` scheiterte schon vor diesen Änderungen unter Linux.
   Wenn rot: hier aufhören. Der Fehler geht vor den Betrieb.
3. **Neue Geheimnisse erzeugen**, in `deploy/secrets/`, Rechte 600:
   - `openssl rand -base64 32 > mail_key.txt` (Postfächer, D-030)
   - die mit age verschlüsselte Kopie legt `backup-setup.sh` in Schritt 6 an, sie kommt mit dem Abholen auf deinen Rechner
   - Rechte wie die übrigen Secrets: `chown 10001:10001 mail_key.txt && chmod 600 mail_key.txt`
   Prüfpunkt: `wc -c mail_key.txt` ergibt 45 (44 Zeichen Base64 und Zeilenende).
4. **Entra ID**, App-Registrierung der Server-Anmeldung:
   - Umleitungs-URI ergänzen: `https://wissen.melchy.cloud/postfaecher/microsoft/rueckruf`
   - API-Berechtigungen: Microsoft Graph, delegiert, `Mail.Read` und `offline_access`, dann Administratorzustimmung erteilen
   - Ablaufdatum des Client-Geheimnisses notieren
   - nur für Outlook.com-Postfächer (D-032): unter Authentifizierung die unterstützten Kontotypen auf „Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten“ stellen, zweite Umleitungs-URI `https://wissen.melchy.cloud/postfaecher/microsoft/privat/rueckruf`. Lehnt das Portal die Umstellung ab, im Manifest zuerst `requestedAccessTokenVersion` auf 2 setzen.
   Prüfpunkt: Unter API-Berechtigungen steht bei beiden „Gewährt für <Mandant>“.

## B. Sicherung zuerst (G03, BETRIEB.md „Sicherung und Wiederherstellungsübung“)

Alle Befehle laufen in PowerShell auf deinem Rechner, im Ordner `%USERPROFILE%\Wissensraum-Sicherung`. In die Shell des VPS wird nichts eingefügt.

5. **Schlüssel**, falls noch nicht vorhanden: `age-keygen -o wissensraum-backup.key` und `ssh-keygen -t ed25519 -N '""' -f wrbackup_ed25519`. Den öffentlichen age-Schlüssel in `deploy/backup-recipient.txt` auf dem VPS.
6. **VPS einrichten und erster Lauf:** `Get-Content wrbackup_ed25519.pub | ssh root@10.13.13.1 bash /opt/docker/wissen/app/deploy/backup-setup.sh --run`.
   Prüfpunkt: Das Journal nennt die Datei und den Audit-Stand (Anzahl und Kopf). In `/var/backups/wissensraum/` liegt eine `.age`-Datei.
7. **Abholen:** `powershell -ExecutionPolicy Bypass -File backup-pull.ps1`.
   Prüfpunkt: Exit 0, die Sicherung liegt unter `sicherungen`, dazu `mail_key.txt.age`.
8. **Übung:** `Get-Content wissensraum-backup.key | ssh root@10.13.13.1 "WR_AGE_IDENTITY=- WR_PG_IMAGE=pgvector/pgvector:pg18 /opt/docker/wissen/app/deploy/restore-drill.sh"`.
   Prüfpunkt: „Ergebnis: bestanden“, gültige Audit-Kette mit demselben Stand wie in Schritt 6.
   Wenn nicht: kein Update. Erst die Sicherung reparieren.
9. **Dauerbetrieb:** `backup-pull.ps1 -Register` und `backup-setup.sh --timer` (Aufruf wie in Schritt 6, mit `--timer` statt `--run`).

## C. Update auf dem VPS

9a. **Vor dem Update:** `cp -p deploy/wissensraum.env deploy/wissensraum.env.vor-update`, `systemctl start wissensraum-backup.service`, `docker tag wissensraum-server:3.0 wissensraum-server:3.0-vor-update`.
    Prüfpunkt: frische Sicherung im Journal, `docker image ls wissensraum-server` zeigt beide Tags mit derselben ID.
10. **Umgebung ergänzen** in `deploy/wissensraum.env` (Vorlage `wissensraum.env.beispiel`):
    - `WR_OIDC_CLIENT_SECRET_EXPIRES=<JJJJ-MM-TT>` aus Schritt 4
    - `WR_EXPORT_DIR=/data/exports`
    - `WR_MAIL_KEY_FILE=/run/secrets/mail_key`
    - `WR_OLLAMA_URL` und `WR_AI_MODEL` prüfen (Messung in Schritt 21)
    Achtung: Ein stehengebliebener Platzhalter `<JJJJ-MM-TT>` verhindert den Start des Servers.
11. **Ollama:** `docker exec <ollama-container> ollama pull qwen3-embedding:0.6b`. Prüfen, ob Ollama im Netz `datenbank` erreichbar ist, sonst das Netz beim Dienst `worker` in `compose.yaml` ergänzen. Ist das Netz intern (`docker network inspect backend -f '{{.Internal}}'` ergibt `true`), für den Download kurz `docker network connect bridge ollama` und danach `docker network disconnect bridge ollama`.
12. **Bauen und starten:** `docker compose -f deploy/compose.yaml up -d --build`.
    Das Image enthält jetzt Tesseract, die Ordner für Export und Logs und braucht das Secret `mail_key`. Der Dienst `migrate` hebt das Schema von 23 auf 26.
    Prüfpunkt: `docker compose -f deploy/compose.yaml ps` zeigt `server` und `worker` als healthy, `migrate` mit Exit 0. Im Worker-Log steht `worker_started` mit `status` `ocr`.
    Wenn `migrate` scheitert: `docker compose logs migrate`. Die Migration rollt vollständig zurück, die Daten bleiben auf Stand 23. Nicht von Hand am Schema ändern.
13. **Alte Logs verwerfen** (S-01): `docker compose -f deploy/compose.yaml up -d --force-recreate server`.
    Prüfpunkt: `docker compose -f deploy/compose.yaml logs server | grep -c 'code='` ergibt 0.
14. **Audit und Integrität:** `docker compose -f deploy/compose.yaml run --rm --no-deps -T server python server.py audit-verify </dev/null`.
    Prüfpunkt: gültige Kette, Anzahl mindestens so groß wie in Schritt 6. In der Weboberfläche unter Verwaltung, Betrieb „Schema, Prüfsummen und Audit-Kette prüfen“: keine Probleme. Die Diagnose zeigt das Client-Geheimnis ohne Warnung.

## D. Funktionsproben im Browser (über WireGuard)

15. **Erreichbarkeit:** ohne VPN 403, mit VPN die Anmeldeseite. Ein Upload über 110 MB endet mit 413 von Traefik.
16. **Semantische Suche:** Verwaltung, Semantische Suche, Einschalten. Prüfpunkt: Der Zähler „Abschnitte mit Vektor“ steigt mit jeder Worker-Runde.
17. **Upload mit Vorschau:** zwei Textdateien und ein Foto einer Textseite hochladen, Vorschau abwarten, eine Datei abwählen, importieren. Prüfpunkt: zwei Dokumente, eins abgewählt, das Foto ist per Texterkennung durchsuchbar.
18. **Postfach:** als Bearbeiter unter Postfächer das eigene Microsoft-Postfach verbinden (bei Hotmail mit `WR_MAIL_PERSONAL_ACCOUNTS=1` über „Privates Microsoft-Postfach verbinden“), Vorschau, eine Nachricht mit Anhang importieren. Prüfpunkt: E-Mail und Anhang sind Dokumente des Kontos, ein zweites Konto sieht sie nicht.
19. **Zwei echte Konten** (offen aus 10.8): ein Leser und ein Bearbeiter. Prüfpunkt: Der Leser sieht nur freigegebene Dokumente, keine fremden Postfächer, keine fremden Importe. Verwaltung gibt ihm 403.
20. **Datenschutz:** Installationsexport anfordern, nach einer Runde herunterladen, löschen. Prüfpunkt: Das ZIP enthält `manifest.json` und keine `account_mailboxes`. „Meine Daten“ liefert die eigene Auskunft.

## E. Messungen

21. **Antwortqualität `qwen3:4b`** (C09), auf deinem Rechner mit exklusivem Ollama, wie in BETRIEB.md. Prüfpunkt: False Reject höchstens 10 %, False Accept höchstens 5 %. Sonst `WR_AI_MODEL=qwen3:8b`.
22. **Antwortzeiten** (B10), von einem Rechner im VPN: Sitzungswert `__Host-wr_session` aus den Entwicklerwerkzeugen in eine Datei legen, dann
    `python tests/bench/server_latency.py --base https://wissen.melchy.cloud --cookie-file <datei>`.
    Das Skript liest nur Suchen und bleibt unter dem Limit von 120 Suchen je Minute. Danach Datei löschen und abmelden.
    Prüfpunkt: Median und P95 notieren. Ziel aus dem Desktop-Benchmark zum Vergleich in `masterplan.md`, Phase 7.3.

## F. Nachtragen

23. In `deploy/BETRIEB.md`: Datum der Kennwortrotation, erster Sicherungslauf, erste Übung, Entra-Zustimmung.
24. In `masterplan.md`: Ergebnisse aus 2, 8, 21 und 22 mit Zahlen. Phasen erst auf ✅, wenn das Gate aus Schritt 2 mit 7 von 7 vorliegt und die Proben aus D bestanden sind.
25. Bedienung mit einem Screenreader (H06), zum Beispiel NVDA unter Windows: Anmeldung, Suche, Upload mit Vorschau, Fragen. Ergebnis in der Paritätsmatrix eintragen.

## Rückweg

Läuft nach Schritt 12 etwas nicht: `docker compose -f deploy/compose.yaml down`, `docker tag wissensraum-server:3.0-vor-update wissensraum-server:3.0`, `cp -p deploy/wissensraum.env.vor-update deploy/wissensraum.env`. Die Migrationen 24 bis 26 fügen nur Spalten und Tabellen hinzu, der alte Server prüft aber die Schemaversion und startet mit Stand 26 nicht. Für einen vollständigen Rückweg die Sicherung aus Schritt 6 einspielen, wie in der Übung aus Schritt 8.
