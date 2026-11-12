# Wissensraum-AI: Roadmap zur Web-Parität

Stand: 11.11.2026, Commit `1298469`, Schema 26. Grundlage: `docs/desktop-web-parity.md` (Matrix, IDs) und `docs/web-architecture-review.md` (Befunde S-xx).

## 1. Leitlinien

- Kleine Phasen, jede mit eigenem PR, eigener Migration falls nötig, Matrix-Gate 7 von 7 und grüner CI.
- Eine Web-Funktion gilt erst als FULL, wenn UI oder API, Persistenz, Rechteprüfung (404 vor 403), Audit, Fehlerfälle und automatisierte Tests nachweislich funktionieren.
- Desktop bleibt unverändert nutzbar. Gemeinsame Änderungen am Kern brauchen Tests für SQLite und PostgreSQL.
- Keine Desktop-Pfadzugriffe im Server. Keine globalen Einstellungen ohne Admin-Prüfung.
- Aufwand in Personentagen (PT) als Spanne. Unsicherheit: niedrig (N), mittel (M), hoch (H).

## 2. Übersicht

| Phase | Ziel | Prio | Aufwand | Unsicherheit | Abhängig von |
|---|---|---|---|---|---|
| 0 | Betrieb absichern (umgesetzt, Einrichtung auf VPS und Windows-Rechner offen) | P0 | 3 bis 5 PT | N | keine |
| 1 | Service-Schicht und atomare Änderungen (umgesetzt, Gate offen) | P1 | 5 bis 8 PT | N | 0 |
| 2 | KI und Suche im Server vollständig (umgesetzt außer C03, C06, Messungen C09 und B10) | P1 | 7 bis 12 PT | M | 1 |
| 3 | Worker-Dienst und Import (umgesetzt, Vorschau mit Abwahl seit 11.11.2026) | P1 | 9 bis 14 PT | M | 1 |
| 4 | Dokumentpflege und Organisation (umgesetzt außer Zahlenwert B05, E03 kam mit Phase 5) | P1/P2 | 18 bis 29 PT | N | 1 |
| 5 | Administration, Datenschutz, Betrieb (umgesetzt, Exportordner auf VPS einrichten) | P2 | 10 bis 15 PT | M | 1, 3 |
| 6 | OCR im Server (umgesetzt, Image-Bau auf dem VPS offen) | P2 | 4 bis 6 PT | M | 3 |
| 7 | Wissensgraph im Web (umgesetzt) | P3 | 10 bis 16 PT | M | 4 |
| 8 | E-Mail-Import (umgesetzt nach D-030, PST nur Desktop, Entra-Freigabe offen) | P3 | 15 bis 25 PT | H | 3, ADR Geheimnisse |
| 9 | Oberfläche und Barrierefreiheit (umgesetzt außer Prüfung mit Screenreader) | P3 | 3 bis 6 PT | N | 4 |
| Summe | | | 84 bis 136 PT | | |

Ohne Phase 8 (E-Mail, PST): 69 bis 111 PT. Die Phasensummen ergeben sich aus den Aufwandsspalten der Matrix plus den Befunden S-xx. Enthalten sind Tests, Gate-Läufe und Doku, nicht die Wartezeit auf Entscheidungen. Zum Vergleich: Masterplan Phase 11 schätzt 49 bis 95 PT für ein erstes Web-Release ohne volle Parität.

## 3. Phasen

### Phase 0: Betrieb absichern (P0)

1. Ziel und Nutzen: keine Geheimnisse in Logs oder Git, Daten wiederherstellbar.
2. Funktionen: E05, G03, S-01, S-02, S-03, S-04.
3. Abhängigkeiten: Zugriff auf VPS und Traefik-Konfiguration.
4. Dateien: `server.py` (`main`), `.gitignore`, `deploy/BETRIEB.md`, neu `deploy/backup.sh` oder Compose-Dienst `backup`.
5. Datenbank: keine Schemaänderung. Lesende Sicherungsrolle oder Eigentümerrolle für `pg_dump`.
6. Sicherheit: schließt S-01, S-02, S-03. S-04 ist Nachweis, kein Code.
7. Schritte:
   1. `uvicorn.run(..., access_log=False)` oder Filter, der die Query von `/auth/callback` schwärzt.
   2. Traefik-Accesslog auf Querystrings prüfen, ggf. Feld abschalten.
   3. Vorhandene Docker-Logs des Dienstes `server` rotieren bzw. leeren (nach Rückfrage).
   4. `.gitignore`: `deploy/secrets/`, `deploy/wissensraum.env`. Auf dem VPS `git status --ignored` und gitleaks über den Arbeitsbaum.
   5. Rotation des alten DB-Kennworts nachweisen und im Masterplan vermerken.
   6. Täglicher `pg_dump -Fc wissensraum_server`, verschlüsselt (age oder gpg) außerhalb des VPS, 14 Tage Aufbewahrung.
   7. Rückspielprobe in `wissensraum_restore_test`, danach `server.py audit-verify` gegen die Kopie.
8. Tests: Unit-Test, dass ein Rückruf mit `code` nicht im erfassten Log steht. Test, dass `git check-ignore` beide Pfade erfasst.
9. Akzeptanz: `docker logs` nach einer Anmeldung enthält kein `code=`. `git check-ignore deploy/secrets/db_app.txt deploy/wissensraum.env` meldet beide. Eine Sicherung existiert außerhalb des VPS, die Rückspielprobe liefert gleiche Zeilenzahlen je Tabelle und `audit-verify` mit `ok: true`.
10. Rollback: Logging-Änderung per Revert. Sicherungsjob abschalten, ohne Daten zu ändern.

### Phase 1: Service-Schicht und atomare Änderungen (P1)

1. Ziel: Rechte, Transaktion und Audit an einer Stelle, keine 500 bei Fachfehlern.
2. Funktionen: C08, H05, S-07, S-08, A11 (Entscheidung), S-06.
3. Abhängigkeiten: Phase 0. Entscheidung des Product Owners zur Dublette (S-05, Varianten a und b).
4. Dateien: neu `services.py`, `server.py`, `web.py`, `audit.py` (Verbindung übergeben), `core.py` (falls Dublettenvariante a).
5. Datenbank: keine Migration für Service-Schicht. Variante a der Dublette braucht eine Änderung am eindeutigen `sha256`-Verhalten und eine Migration.
6. Sicherheit: Audit lückenlos, keine verwaisten Dokumente, Rate Limits für Suche, Fragen, Upload.
7. Schritte:
   1. `audit.record(db=...)` innerhalb der Fachtransaktion.
   2. `services.create_document`, `change_metadata`, `ask`, `grant` mit Konto, Rechten, Transaktion, Audit.
   3. `server.py` und `web.py` auf die Dienste umstellen.
   4. `ProviderError` und Budgetgrenze als 503 mit Meldung, Audit-Status `unavailable`.
   5. `RateLimit` je Konto für Fragen, Upload, Suche.
   6. Dublettenverhalten nach Entscheidung.
8. Tests: Fehlerinjektion im Audit lässt keine Änderung zurück. Fehlerinjektion in der Freigabe lässt kein Dokument zurück. KI-Ausfall ergibt 503 und Audit. Grenzwerte der Rate Limits ergeben 429.
9. Akzeptanz: alle bestehenden 45 Server-Tests grün, neue Tests grün, kein Schreibvorgang mit Audit außerhalb von `store.transaction()`.
   Umsetzung 10.11.2026: Die vier doppelten Abläufe (Dokument anlegen, Metadaten, Freigabe, Konto anlegen) und die Frage liegen in `services.py`. Sammlung ändern, Entität umbenennen, Konto ändern, Freigabe entfernen und Abmelden sind nur an einer Stelle vorhanden und laufen dort direkt in `store.transaction()`. Dubletten bleiben nach Entscheidung vom 10.11.2026 unverändert (409 ohne Zugriff).
10. Rollback: Revert des PR. Keine Datenmigration außer bei Dublettenvariante a, dafür Rückwärtsmigration mit Sicherung.

### Phase 2: KI und Suche im Server vollständig (P1)

1. Ziel: hybride Suche und belegte Antwortqualität im Server.
2. Funktionen: B02, B04, G08, C02, C03, C06, C09, B10.
3. Abhängigkeiten: Phase 1. Für den Einbettungslauf ein Hintergrundprozess (minimal als Thread im Serverprozess, sauberer mit Phase 3).
4. Dateien: `core.py` (`EMBEDDING_PROVIDERS`, `Embedder`), `server.py` (`settings_from_env`), `vector_sync.py`, `web.py`, `templates/search.html`, `templates/answer.html`, `templates/questions.html`.
5. Datenbank: keine Migration. Einstellung `vectors` in `settings`.
6. Sicherheit: Einbettung nur über internes Netz. Aktivierung nur durch Admin mit Audit `settings_changed`.
7. Schritte:
   1. Embedding-Endpunkt aus `WR_OLLAMA_URL` ableiten. Modell über `WR_EMBEDDING_MODEL`.
   2. Admin-Schalter für semantische Suche mit Vektorstand.
   3. Einbettung im Hintergrund nach Upload.
   4. Suchstatus und Quellenlage anzeigen, Quellen im Verlauf.
   5. Messung mit eingefrorenem Bestand V2 für `qwen3:4b` oder Rückkehr zu `qwen3:8b`. Speicher auf dem VPS prüfen.
   6. Antwortzeiten von Suche und Frage auf dem VPS messen.
8. Tests: Endpunkt aus Umgebung, Status `Hybride Suche` mit Fake-Embedder, Rechtefilter in der Vektorsuche über den Server (I1 schon im Kern, hier Endpunkt), Admin-Schalter E1.
9. Akzeptanz: Server meldet `mode: hybrid`, wenn Vektoren vollständig sind. False Accept und False Reject des eingesetzten Modells liegen innerhalb der Gate-Grenzen von Release 2.1 oder das Modell wird gewechselt. Median und p95 für Suche und Frage sind dokumentiert.
10. Rollback: Schalter aus, Suche fällt auf Volltext zurück. Vektoren bleiben gespeichert.

### Phase 3: Worker-Dienst und Import (P1)

1. Ziel: lange Aufgaben ohne blockierende HTTP-Anfrage, Mehrfach-Upload mit Vorschau.
2. Funktionen: A03, D02, D03, D11 (Anzeige), H04. Ersatz für D01.
3. Abhängigkeiten: Phase 1.
4. Dateien: neu `worker.py`, `import_jobs.py` (Konto am Auftrag), `web.py`, neue Vorlagen, `deploy/compose.yaml` (Dienst `worker`).
5. Datenbank: Migration 24: `import_jobs.account_id`, Upload-Zwischenablage als Bytea oder Volume mit Prüfsumme.
6. Sicherheit: Aufträge nur für eigenes Konto sichtbar, Admin sieht alle. ZIP mit bestehenden Dekompressionsgrenzen. Keine Pfade aus Benutzereingabe.
7. Schritte: Auftragstabelle mit Konto, Worker mit Sperre (`FOR UPDATE SKIP LOCKED`), Mehrfach- und ZIP-Upload, Vorschau, Start, Fortschritt per htmx-Polling, Abbruch, Fortsetzen, Bericht.
8. Tests: Wettlauf zweier Worker, Abbruch, Fortsetzen nach Neustart, fremder Auftrag 404, ZIP-Bombe abgelehnt, Besitzerfreigabe je Dokument.
9. Akzeptanz: 200 Dateien in einem ZIP werden ohne HTTP-Zeitüberschreitung importiert, Fortschritt sichtbar, Neustart des Workers setzt fort, Fehlerjournal je Datei.
10. Rollback: Dienst `worker` stoppen. Migration 24 rückwärts nur mit Sicherung.

### Phase 4: Dokumentpflege und Organisation (P1/P2)

1. Ziel: Desktop-Funktionen zur Pflege im Web.
2. Funktionen: A01, A05 bis A09, A12 bis A14, B05 bis B08, F02 bis F05, C04, E03 (Löschung durch Besitzer).
3. Abhängigkeiten: Phase 1.
4. Dateien: `services.py`, `web.py`, Vorlagen, `core.py` (Konto für Favoriten und gespeicherte Suchen).
5. Datenbank: Migration 25: `favorites.account_id`, `saved_searches.account_id` mit Übernahme bestehender Zeilen für den ersten Administrator.
6. Sicherheit: Löschen nur Besitzer oder Admin, Audit `document_deleted`. Sammlung anlegen gibt Besitzerfreigabe. Alle Formulare mit Revision.
7. Schritte: Tags, Status, Verantwortlicher, Wiedervorlage, Verlauf, Löschen, Favoriten und gespeicherte Suchen je Konto, Filter, Sammlungen anlegen, umbenennen, zuordnen, löschen, Beziehungen, Fassungsvergleich, Zusammenfassung.
8. Tests: je Funktion I1, I3, E1, T2 und R1 analog zu `test_rights` und `test_web`.
9. Akzeptanz: jede Zeile der Phase steht in der Matrix auf FULL mit benanntem Test.
10. Rollback: Revert je PR. Migration 25 rückwärts mit Sicherung.

### Phase 5: Administration, Datenschutz, Betrieb (P2)

1. Ziel: Admin kann den Server ohne Shell überblicken.
2. Funktionen: G01, G02, G07, C05, D13, E02, E04, D11 (Verwaltung), S-10, S-11.
3. Abhängigkeiten: Phase 1, für lange Exporte Phase 3.
4. Dateien: `operations.py`, `backups.py` (PostgreSQL-Integrität), `privacy.py`, `retention.py`, `budgets.py`, `rules_view` als Web-Seite, `web.py`.
5. Datenbank: keine Migration erwartet.
6. Sicherheit: nur Admin, jede Einstellungsänderung mit Audit `settings_changed`, Datenschutzexport als Auftrag mit Audit, keine Inhalte in der Betriebsübersicht.
7. Schritte: Betriebsübersicht, Integrität, Budgets, Aufbewahrung, Regeln, Chunk-Strategie, Datenschutzexport, Ablaufwarnung Client-Geheimnis.
8. Tests: E1 je Seite, Audit je Änderung, Export enthält nur Daten der Installation.
9. Akzeptanz: alle Admin-Funktionen ohne Shell erreichbar, Audit-Kette nach Änderungen gültig.
10. Rollback: Revert.

### Phase 6: OCR im Server (P2)

1. Ziel: Scans und Bilder importieren.
2. Funktionen: D04, D05.
3. Abhängigkeiten: Phase 3.
4. Dateien: `Dockerfile` (Worker mit `tesseract-ocr`, `-deu`, `-eng`), `worker.py`, `ocr.py`.
5. Datenbank: keine.
6. Sicherheit: Bildpunkt- und Seitengrenzen aus `ocr.py`, Speicherlimit des Workers.
7. Schritte: Worker-Image mit Tesseract, Upload von Bildern zulassen, Quarantäne erneut verarbeiten.
8. Tests: vorhandene 10 OCR-Tests im Worker-Image ausführen (heute übersprungen), Grenzwerttests.
9. Akzeptanz: OCR-Tests laufen ohne Übersprung, ein PDF-Scan wird durchsuchbar.
10. Rollback: Bilder wieder ablehnen.

### Phase 7: Wissensgraph im Web (P3)

1. Funktionen: F06 bis F11. 2. Abhängigkeit: Phase 4. 3. Dateien: `graph_view.py` (Layout ohne Tk herauslösen), `web.py`, Vorlagen mit serverseitigem SVG. 4. Datenbank: keine. 5. Sicherheit: Entitäten nur mit sichtbarem Beleg (vorhanden), Änderungen nur Admin oder Schreibrecht am Belegdokument, Audit `graph_changed`. 6. Akzeptanz: alle Graphfunktionen der CLI im Web mit I1, E1, T2. 7. Rollback: Revert. Aufwand 10 bis 16 PT.

### Phase 8: E-Mail- und PST-Import (P3, hohe Unsicherheit)

Voraussetzung ist eine Entscheidung (ADR) zur Ablage von Postfachzugängen je Konto im Server: verschlüsselt in der Datenbank mit Schlüssel als Docker-Secret, oder delegierter Microsoft-Graph-Zugriff je Benutzer mit Token im Sitzungskontext. Ohne diese Entscheidung nicht beginnen. Funktionen D06 bis D10, D12. PST als Upload in den Worker mit `libpff`. Aufwand 15 bis 25 PT.

### Phase 9: Oberfläche (P3)

H02, H03, H06, W13. Dunkelmodus über `prefers-color-scheme` und Umschalter, Logo, Media Queries, Tastaturbedienung und Kontrast prüfen. Aufwand 3 bis 6 PT.

## 4. Empfohlene Reihenfolge

0, 1, 2, 3, 4, dann 5 und 6 parallel möglich, danach 7, 9 und zuletzt 8.

Begründung: Phase 0 schließt die einzigen P0-Befunde. Phase 1 verhindert, dass jede weitere Funktion die Audit- und Transaktionslücke kopiert. Phase 2 macht die vorhandene Suche im Server erst wirksam. Phase 3 ist die technische Voraussetzung für Import, OCR und E-Mail.

## 5. Offene Entscheidungen für den Product Owner

1. Zielumfang der Parität (Masterplan 11.3 verlangt eine „vereinbarte“ Parität). Vorschlag: Phasen 0 bis 5 als Muss, 6 bis 9 als Kann.
2. Dublettenverhalten über Kontogrenzen (S-05).
3. Eingesetztes Modell: `qwen3:4b` messen oder `qwen3:8b` betreiben.
4. Verhalten des KI-Verlaufs nach Entzug einer Freigabe (S-10).
5. Geheimnisablage für Postfachzugänge (Phase 8).
