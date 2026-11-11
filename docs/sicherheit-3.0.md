# Release 3.0: Bedrohungsmodell und Architektur

Stand 08.11.2026, Schritt 10.1. Grundlage für 10.2 bis 10.8. Änderungen an diesem Dokument brauchen die Zustimmung des Product Owners.

## 1. Entscheidungen (D-028)

| Thema | Entscheidung |
|---|---|
| Betriebsform | Eigener Webserver (FastAPI, serverseitig gerendert mit HTMX, D-012) per Docker auf dem IONOS-VPS unter `wissen.melchy.cloud`, erreichbar nur über WireGuard (D-029, 09.11.2026; ursprünglich Heimnetz oder Unraid). Öffentliche Erreichbarkeit erst mit Release 3.1. |
| Desktop | Die Tkinter-Anwendung bleibt Einzelplatzversion mit SQLite oder PostgreSQL und ändert ihr Verhalten nicht. Ein Webserver und eine Desktopinstanz schreiben nie gleichzeitig in dieselbe Datenbank. |
| Datenbank des Servers | Nur PostgreSQL 18. SQLite bleibt der Desktop vorbehalten. |
| Anmeldung | OIDC mit Microsoft Entra ID, Authorization Code Flow mit PKCE, vertraulicher Client. Der Code bleibt anbieterneutral, Tests laufen gegen einen lokalen Test-Identitätsanbieter, nie gegen Microsoft. |
| Mandanten | Eine Organisation je Installation. Die Organisations-Kennung steht im Datenmodell, aktiv ist genau eine. |
| Rollen | Administrator, Bearbeiter, Leser. Rechte zusätzlich je Sammlung und je Dokument. |

## 2. Akteure und Vertrauensgrenzen

| Akteur | Vertrauen | Annahme |
|---|---|---|
| Administrator | hoch | verwaltet Benutzer, Rollen, Rechte, Einstellungen, Sicherungen |
| Bearbeiter | mittel | importiert, ändert Metadaten und Sammlungen im Rahmen seiner Rechte |
| Leser | gering | sucht, liest und fragt nur in freigegebenen Beständen |
| Angreifer im Heimnetz | keins | erreicht den Server, hat aber kein Konto |
| Böswilliger Benutzer | gering | hat ein gültiges Konto und versucht, fremde Inhalte zu sehen oder zu ändern |
| Importierte Inhalte | keins | Dokumente, E-Mails und Anhänge sind Daten, nie Anweisungen (bestehende Regel) |
| KI-Anbieter | begrenzt | erhält nur Abschnitte, die der Fragende sehen darf |
| Microsoft Entra ID | hoch für Identität | liefert signierte Tokens, entscheidet nicht über Rechte im Wissensraum |

Vertrauensgrenzen: Browser zu Server (HTTPS), Server zu PostgreSQL (internes Netz), Server zu Entra ID (HTTPS), Server zu KI-Anbieter (HTTPS oder lokales Ollama), Server zu Dateiablage.

## 3. Schützenswerte Werte

Dokumenttexte und Originale, Abschnitte und Vektoren (sie verraten Inhalte), KI-Verlauf (enthält Zitate), Wissensgraph mit Belegen, Metadaten und Sammlungsnamen, Sitzungen, Audit-Verlauf, Geheimnisse (Entra-Client-Geheimnis, KI-Schlüssel, Datenbankkennwort, Postfachzugänge), Sicherungen.

## 4. Sicherheitsarchitektur

### Anmeldung (10.3)

- Authorization Code mit PKCE (S256), `state` und `nonce` je Anmeldung, einmal verwendbar, 10 Minuten gültig.
- Geprüft werden Signatur über die JWKS des Mandanten, `iss` mit der konfigurierten Mandanten-ID, `aud` gleich Client-ID, `exp`, `iat`, `nbf` mit höchstens 2 Minuten Uhrabweichung, `nonce`.
- Zuordnung eines Kontos über `tid` und `oid`, nie über E-Mail-Adresse oder Anzeigename.
- Keine Selbstregistrierung. Ein Konto entsteht nur, wenn ein Administrator es vorher anlegt. Den ersten Administrator legt ein Befehl auf dem Server an.
- Fehlgeschlagene Anmeldungen werden je Quelle begrenzt und protokolliert.

### Sitzungen (10.3)

- Serverseitig in PostgreSQL. Die Sitzungskennung hat 256 Bit Zufall, gespeichert wird nur ihr SHA-256.
- Cookie `__Host-wr_session`: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`.
- Inaktivität 30 Minuten, absolute Dauer 8 Stunden. Neue Kennung bei jeder Anmeldung. Abmelden, Rollen- oder Rechteänderung und Sperren durch einen Administrator widerrufen alle Sitzungen des Kontos.
- HTTPS ist Pflicht, auch im Heimnetz. Unverschlüsseltes HTTP nur auf `localhost` im Entwicklungsmodus.

### CSRF und Browser-Schutz (10.3)

- Synchronizer-Token je Sitzung für jede zustandsändernde Anfrage, zusätzlich Prüfung von `Origin` bzw. `Referer` gegen die konfigurierte Adresse.
- Zustandsänderungen nur über POST, PUT, PATCH und DELETE.
- Header: `Content-Security-Policy` ohne Inline-Skripte, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, mit HTTPS `Strict-Transport-Security`.
- Rate Limits für Anmeldung, Suche, KI-Fragen und Uploads.

### Rechte (10.2, 10.4)

- Rollen: Administrator darf alles. Bearbeiter darf importieren und ändert Dokumente und Sammlungen, für die er Schreibrecht hat. Leser darf nur lesen, suchen und fragen.
- Freigaben vergeben Lesen oder Schreiben an ein Konto, je Sammlung oder je Dokument. Schreiben wirkt nur bis zur Obergrenze der Rolle, ein Leser schreibt also nie.
- Ein Dokument ist sichtbar für: Administratoren, das importierende Konto, Konten mit Freigabe auf das Dokument, Konten mit Freigabe auf eine Sammlung, die das Dokument enthält.
- Bestehende Dokumente gehören nach der Migration dem ersten Administrator und sind für andere unsichtbar, bis er sie freigibt.
- Jede Abfrage filtert die Rechte in der Datenbank, bevor sortiert und begrenzt wird. Das gilt für Liste, Volltext, Vektorsuche, Graph, Original-Download, Export und KI-Kontext. Ein nachträgliches Ausfiltern der ersten Treffer ist verboten, weil es über Anzahl und Ränge fremde Inhalte verrät.
- Eine KI-Antwort bekommt nur Abschnitte, die der Fragende sehen darf. Der KI-Verlauf ist je Konto getrennt.
- Wissensgraph: Entitäten und Relationen sind nur sichtbar, wenn mindestens ein Beleg in einem sichtbaren Dokument liegt. Belege aus unsichtbaren Dokumenten werden nie angezeigt.
- Nicht vorhandene und nicht sichtbare Objekte geben dieselbe Antwort (404), damit Kennungen nicht abgetastet werden können.

### Audit-Verlauf (10.5)

- Eigene Tabelle, nur anhängen. Jeder Eintrag trägt den SHA-256 des vorherigen Eintrags und seines eigenen Inhalts in kanonischer Form.
- Die Datenbankrolle des Servers darf dort nur einfügen und lesen. Eine Prüfung über die Kette meldet die erste veränderte oder fehlende Stelle.
- Erfasst werden Anmeldung, Abmeldung, abgelehnte Anmeldung, Sperren, Rollen- und Rechteänderungen, Import, Löschung, Export, Original-Download, Datenschutzlöschung, Einstellungsänderungen und KI-Fragen. Gespeichert werden Kennungen, Zeitpunkt, Konto und Quelle, nie Dokumenttexte, Fragen oder Antworten.

### Gleichzeitige Bearbeitung (10.6)

- Versionsnummer an Dokument-Metadaten, Sammlungen, Freigaben und Graphobjekten. Eine Änderung nennt die gelesene Version. Weicht sie ab, antwortet der Server mit 409 und zeigt den aktuellen Stand. Nichts wird still überschrieben.

### Geheimnisse (10.8)

- Der Server liest Geheimnisse aus Docker-Secrets-Dateien oder Umgebungsvariablen, nie aus Repository, Datenbank oder Logs. Logs maskieren sie wie bisher.
- Ausnahme Postfach-Zugänge (D-030, `docs/adr-postfach-zugaenge.md`): IMAP-Kennwörter und Microsoft-Refresh-Token je Konto liegen mit AES-256-GCM verschlüsselt in PostgreSQL, Konto und Postfach als zusätzliche Daten der Verschlüsselung. Der Schlüssel ist das Docker-Secret `mail_key`, nur Server und Worker lesen es, eine mit age verschlüsselte Kopie liegt außerhalb des VPS. Chiffrate erscheinen nie in Log, Audit, Auskunft oder Installationsexport.
- Zwei Datenbankrollen (D-029): Die Eigentümerrolle migriert, die Laufzeitrolle des Servers hat kein DDL-Recht, liest die Migrationshistorie nur und darf den Audit-Verlauf nur lesen und ergänzen.
- Das Entra-Client-Geheimnis hat ein Ablaufdatum, die Betriebsübersicht warnt 30 Tage vorher.

## 5. Bedrohungen und Gegenmaßnahmen (STRIDE)

| Nr. | Bedrohung | Gegenmaßnahme | Nachweis |
|---|---|---|---|
| S1 | Anmeldung mit gefälschtem oder fremdem Token | Signatur, `iss`, `aud`, `nonce`, Zuordnung über `tid` und `oid` | Negativtests mit falscher Signatur, fremdem Mandanten, falschem Publikum, wiederverwendetem `nonce` |
| S2 | Sitzungsdiebstahl oder Fixierung | Zufallskennung, nur Hash gespeichert, neue Kennung je Anmeldung, `Secure` und `HttpOnly` | Test: alte Kennung nach Anmeldung ungültig, Cookie-Attribute |
| S3 | Brute Force auf Anmeldung | Rate Limit, Audit | Test: Sperre nach Grenzwert |
| T1 | Manipulierter Audit-Verlauf | Hash-Kette, nur Einfügen | Test: geänderter, gelöschter, vertauschter Eintrag wird erkannt |
| T2 | Verlorene Änderung bei gleichzeitiger Bearbeitung | Versionsnummer, 409 | Test zweier Bearbeiter |
| R1 | Abstreiten einer Änderung oder eines Exports | Audit mit Konto und Zeitpunkt | Test je Ereignisart |
| I1 | Fremde Dokumente über Liste, Suche, Vektoren, Graph oder Download | Rechtefilter in der Datenbank vor Sortierung | Negativtests je Endpunkt mit zweitem Konto |
| I2 | Fremde Inhalte in KI-Antwort | KI-Kontext nur aus sichtbaren Abschnitten | Test: Frage nach fremdem Inhalt liefert keine Quelle daraus |
| I3 | Abtasten von Kennungen | Einheitlich 404 | Test: fremde und fehlende Kennung gleiche Antwort |
| I4 | Geheimnisse in Logs, Fehlermeldungen, Export | Maskierung, keine Geheimnisse in der Datenbank | Secret-Scan, Test der Fehlerantworten |
| D1 | Überlastung durch große Uploads oder viele KI-Fragen | bestehende Größengrenzen, Rate Limits, Budgets aus 9.2 | Tests der Grenzen |
| E1 | Leser schreibt, Bearbeiter wird Administrator | Rollenprüfung serverseitig je Endpunkt | Negativtests je Endpunkt und Rolle |
| E2 | CSRF über fremde Webseite | Synchronizer-Token, `Origin`-Prüfung, `SameSite` | Test ohne und mit falschem Token |
| E3 | Prompt Injection über importierte Inhalte | Inhalte bleiben Daten, KI-Antworten ändern keine Rechte und lösen keine Aktionen aus | bestehende Tests plus Test, dass eine Antwort keine Serveraktion auslöst |

## 6. Nicht Teil von Release 3.0

Öffentliche Erreichbarkeit, eigene Domain und VPS (Release 3.1), mehrere Organisationen, öffentliche Registrierung, anonyme Freigabelinks, mobile App, Synchronisation zwischen Desktop- und Serverbestand.

## 7. Betriebsprozess (10.8)

- Patches: wöchentlich `pip-audit` und Prüfung der Container-Basis. Kritische Befunde innerhalb von 72 Stunden beheben oder den Dienst abschalten.
- Incident: Verdacht melden, betroffene Sitzungen widerrufen, Geheimnisse rotieren, Audit-Kette prüfen, Sicherung vor jeder Bereinigung, Ursache und Maßnahme im Masterplan festhalten.
- Sicherung: tägliche PostgreSQL-Sicherung, Wiederherstellungsübung vierteljährlich wie in 9.4.
