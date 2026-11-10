# Wissensraum-AI: Funktionsparität Desktop und Web

Stand: 10.11.2026. Analysephase, keine Codeänderung.

## 1. Grundlage und Methode

| Punkt | Wert |
|---|---|
| Analysierter Stand | GitHub `Melchy1971/Wissensraum-AI`, Branch `main`, Commit `bf3b126` (Merge PR #46, 09.11.2026 14:42) |
| Nicht analysiert | Arbeitsbaum auf dem VPS unter `/opt/docker/wissen/app`. Lokale Änderungen dort, `deploy/wissensraum.env`, Secrets, Traefik- und WireGuard-Konfiguration sind nicht geprüft. Abweichungen vom GitHub-Stand sind möglich. |
| Desktop-Quellen | vollständig vorhanden: `app.py` (Tkinter), `cli.py`, Fachkern `core.py` und Module. Die vergleichende Bewertung ist damit zulässig. |
| Web-Quellen | vollständig vorhanden: `server.py` (FastAPI, API und Anmeldung), `web.py` mit `templates/` und `static/` (Jinja2 und htmx), `accounts.py`, `sessions.py`, `oidc.py`, `audit.py`, `Dockerfile`, `deploy/` |
| Testlauf Python 3.13 | 609 Tests, 3 Importfehler (`test_gui`, `test_configuration`, `test_logging_integration`: kein `tkinter` im Interpreter), 60 übersprungen. Nicht aussagekräftig für die GUI. |
| Testlauf Python 3.12 mit Tk unter Xvfb | 728 Tests, 1 Fehlschlag, 59 übersprungen (46 PostgreSQL nicht konfiguriert, 10 Tesseract fehlt, 3 nur Windows). Fehlschlag `test_settings_pages_scroll_to_their_last_widget_at_minimum_size (page='mail')`: Widget 1 Pixel breiter als die Seite. Vermutlich Schriftmetrik von Xvfb, nicht bestätigt. |
| PostgreSQL-Vertrag | `tests.test_postgres` gegen einen Wegwerf-Cluster PostgreSQL 16.x (nicht 18) auf Port 55432: 49 von 49 bestanden. PostgreSQL 18 wurde nicht geprüft. |
| Server-Tests einzeln | `test_server`, `test_web`, `test_rights`, `test_audit`, `test_concurrency`, `test_accounts`: 45 von 45 bestanden. |
| Nicht ausgeführt | lokales Matrix-Gate (7 Jobs), Windows, Tesseract-Tests, Browser-Ende-zu-Ende mit echter Entra-Anmeldung, Last- und Antwortzeitmessung, Antwortqualität mit `qwen3:4b` |

Bewertungsregel: FULL nur, wenn UI oder API, Backend, Persistenz, Rechteprüfung und mindestens ein automatisierter Test nachweisbar sind. Ein vorhandener Codepfad ohne Web-Zugang zählt als PARTIAL oder MISSING.

## 2. Korrekturen an der Ausgangsbeschreibung

| Annahme im Auftrag | Befund im Code |
|---|---|
| Web mit React und TypeScript | Kein React, kein TypeScript. Serverseitig gerendert mit Jinja2 und htmx 2.0.11 (D-012). Masterplan 11: SPA nur bei nachgewiesenem Bedarf. |
| PostgreSQL 18 mit pgvector | pgvector wird nicht genutzt. Vektoren liegen als float16-Bytes in `chunk_vectors.vector`, die Suche läuft exakt mit numpy über eine prozessweite Matrix (`core.VectorIndex.vector_search`, `VECTOR_CACHE`). Masterplan Zeile 1103: pgvector verschoben (D-016). |
| Rocky Linux 10 | `deploy/BETRIEB.md` nennt Rocky Linux 9. Dokumentationsabweichung. |
| Ollama mit `qwen3:4b` | `deploy/wissensraum.env.beispiel` setzt `qwen3:8b`. Alle Qualitätsmessungen (False Reject, False Accept) stammen von `qwen3:8b`. Für `qwen3:4b` gibt es keinen Nachweis. |

## 3. Inventur der Einstiegspunkte

| Anwendung | Einstieg | Oberfläche | Datenbank |
|---|---|---|---|
| Desktop | `app.py` `main()`, `start.sh`, `start_windows.bat` | Tkinter, 6 Reiter, 9 Einstellungsseiten | SQLite oder PostgreSQL, Profil über `database.py` |
| Desktop-CLI | `cli.py` | Kommandozeile, rund 60 Unterbefehle | wie Desktop, ohne Konto (sieht alles) |
| Web | `server.py` `main()`, Container `wissensraum-server:3.0` | `web.py` Seiten unter `/`, `/suche`, `/dokument/{id}`, `/fragen`, `/sammlungen`, `/hochladen`, `/verwaltung`, JSON unter `/api` | nur PostgreSQL, eigene Datenbank `wissensraum_server` |
| Server-CLI | `server.py migrate`, `account-add`, `audit-verify` | Kommandozeile im Container | Eigentümer- bzw. Laufzeitrolle |

Gemeinsamer Kern: `core.py` (`Store`, `PostgresStore`, `AI`, `Embedder`, Extraktion), `retrieval.py`, `evidence.py`, `rules.py`, `migrations.py`, `budgets.py`. Der Server nutzt denselben Kern über `Store.as_viewer(konto)`. Schema-Version 23 gilt für beide.

## 4. Kennzahlen

| Status | Anzahl Desktop-Funktionen |
|---|---:|
| FULL | 11 |
| PARTIAL | 19 |
| MISSING | 36 |
| DIFFERENT | 3 |
| NOT_APPLICABLE | 7 |
| UNKNOWN | 2 |
| Summe Desktop-Funktionen | 78 |
| WEB_ONLY (zusätzlich) | 13 |

Funktionsparität = FULL / (FULL + PARTIAL + MISSING + DIFFERENT) = 11 / 69 = 15,9 % (Stand nach Phase 11.0, Branch `phase-11-0-betrieb`. Vor Phase 11.0: 10 / 69 = 14,5 %).

## 5. Funktionsmatrix

Spalte Desktop: Status der Referenz (GUI oder CLI). Spalte Web: Status im Web. Aufwand in Personentagen (PT), Spanne für Unsicherheit.

| ID | Funktionsbereich | Funktion | Desktop | Web | Backend | Tests | Priorität | Aufwand |
|---|---|---|---|---|---|---|---|---|
| A01 | Dokumente | Notiz bzw. Textdokument erfassen | FULL (GUI) | PARTIAL | `Store.add`, `POST /api/documents` | test_rights e1 | P2 | 0,5 bis 1 |
| A02 | Dokumente | Einzeldatei hochladen (Textformate) | FULL (GUI) | FULL | `extract_document`, `POST /hochladen` | test_web upload | erledigt | 0 |
| A03 | Dokumente | Mehrere Dateien mit Importvorschau | FULL (GUI) | MISSING | `ImportJobs.preview_*` | keine Web-Tests | P1 | 3 bis 5 |
| A04 | Dokumente | Titel und Bereich bearbeiten | FULL (GUI, CLI) | FULL | `update_metadata`, `PATCH /api/documents/{id}`, `POST /dokument/{id}/metadaten` | test_web, test_concurrency | erledigt | 0 |
| A05 | Dokumente | Tags bearbeiten | FULL (GUI, CLI) | MISSING | `update_metadata(add_tags, remove_tags)` | keine Web-Tests | P2 | 1 bis 2 |
| A06 | Dokumente | Status, Verantwortlicher, Wiedervorlage | FULL (GUI, CLI) | PARTIAL | `update_metadata(status, owner, review_date)` | keine Web-Tests | P2 | 1 bis 1,5 |
| A07 | Dokumente | Metadatenverlauf anzeigen | FULL (GUI, CLI) | MISSING | `metadata_history`, Tabelle `metadata_changes` | keine Web-Tests | P2 | 0,5 bis 1 |
| A08 | Dokumente | Fällige Wiedervorlagen | FULL (CLI) | MISSING | `due_reviews` | keine Web-Tests | P3 | 0,5 bis 1 |
| A09 | Dokumente | Dokument löschen | FULL (GUI, CLI) | MISSING | `Store.delete`, Audit-Ereignis `document_deleted` definiert, kein Endpunkt | keine Web-Tests | P1 | 1,5 bis 2,5 |
| A10 | Dokumente | Original herunterladen | FULL (GUI) | FULL | `GET /api/documents/{id}/original` | test_rights i3, test_audit r1 | erledigt | 0 |
| A11 | Dokumente | Dublettenprüfung | FULL | DIFFERENT | `Store.add` prüft `sha256` global | test_web upload (409) | P1 | 2 bis 4 |
| A12 | Dokumente | Fassungslinie beim erneuten Import | FULL (GUI) | MISSING | `Store.add(versioned=True, source)` | keine Web-Tests | P2 | 1 bis 2 |
| A13 | Dokumente | Fassungen anzeigen und vergleichen | FULL (GUI, CLI) | PARTIAL | `versions` im JSON von `GET /api/documents/{id}`, `compare` fehlt | test_rights | P2 | 1 bis 2 |
| A14 | Dokumente | Favoriten | FULL (GUI, CLI) | MISSING | Tabelle `favorites` ohne Konto | keine | P2 | 1,5 bis 2,5 |
| A15 | Dokumente | Dokumentinhalt anzeigen | FULL (GUI) | FULL | `GET /dokument/{id}`, `GET /api/documents/{id}` | test_web, test_rights i1 | erledigt | 0 |
| B01 | Suche | Volltextsuche mit Rechtefilter | FULL (GUI, CLI) | FULL | `Store.search`, `GET /suche`, `GET /api/search` | test_rights i1, test_web | erledigt | 0 |
| B02 | Suche | Semantische und hybride Suche (RRF) | FULL (GUI, CLI) | PARTIAL | `Retriever`, `vector_search`, `Embedder` | test_retrieval (Kern), keine Web-Tests | P1 | 3 bis 5 |
| B03 | Suche | Fundstelle mit Abschnitt | FULL (GUI) | FULL | `retrieval.snippet` | test_web | erledigt | 0 |
| B04 | Suche | Suchstatus (Modus, Fallback, Vektorstand) | FULL (GUI) | PARTIAL | `Retrieval.status`, API liefert nur `mode` | keine Web-Tests | P2 | 0,5 |
| B05 | Suche | Ränge und Score anzeigen | FULL (GUI) | MISSING | `Retrieval.results` | keine | P3 | 0,5 |
| B06 | Suche | Tag-Filter | FULL (GUI, CLI) | MISSING | `search(tag=)`, `list(tag=)` | keine Web-Tests | P2 | 1 |
| B07 | Suche | Filter Sammlung und Favoriten | FULL (GUI, CLI) | MISSING | `list(collection=, favorites=)` | keine Web-Tests | P2 | 1 |
| B08 | Suche | Gespeicherte Suchen | FULL (GUI, CLI) | MISSING | Tabelle `saved_searches` ohne Konto | keine | P2 | 1,5 bis 2 |
| B09 | Suche | Dokumentliste mit Seiten | FULL (GUI, CLI) | FULL | `Store.list`, `GET /`, `GET /api/documents` | test_web, test_rights | erledigt | 0 |
| B10 | Suche | Antwortzeiten unter Last auf dem VPS | gemessen (Benchmark Desktop) | UNKNOWN | `tests/bench/benchmark.py` | keine Messung Server | P2 | 1 bis 2 |
| C01 | KI | Frage mit Quellen und Evidenzprüfung | FULL (GUI, CLI) | FULL | `AI.ask`, `POST /fragen`, `POST /api/questions` | test_web, test_rights i2 | erledigt | 0 |
| C02 | KI | Quellenlage und Ablehnungsgrund anzeigen | FULL (GUI) | PARTIAL | `AnswerResult.assessment` nicht ausgegeben | keine | P2 | 0,5 bis 1 |
| C03 | KI | KI-Verlauf | FULL (GUI) | PARTIAL | `Store.history` je Konto, Quellen nicht angezeigt | test_web | P2 | 0,5 bis 1 |
| C04 | KI | Dokument zusammenfassen | FULL (GUI) | MISSING | `AI.summarize` | keine Web-Tests | P2 | 1 bis 1,5 |
| C05 | KI | Monatsbudget je Anbieter | FULL (GUI, CLI) | PARTIAL | `budgets.check` wirkt, keine Verwaltung | keine Web-Tests | P2 | 1 |
| C06 | KI | Veraltete Dokumente aus Antworten ausschließen | FULL (CLI) | PARTIAL | `exclude_retired` wirkt, Schalter und Status fehlen | keine Web-Tests | P2 | 0,5 |
| C07 | KI | Graphkontext in Antworten | aus (D-025) | NOT_APPLICABLE | `set_graph_context` | test_graph_context | keine | 0 |
| C08 | KI | Fehler bei KI-Ausfall oder Budgetgrenze | FULL (GUI) | DIFFERENT | `ProviderError` unbehandelt in `web.ask` und `server.question` | keine | P1 | 0,5 bis 1 |
| C09 | KI | Qualitäts-Gate mit eingesetztem Modell | gemessen mit `qwen3:8b` | DIFFERENT | `tests/eval/evaluate.py` | keine Messung `qwen3:4b` | P1 | 1 bis 2 |
| C10 | KI | Anbieter, Modell und Schlüssel in der Oberfläche | FULL (GUI) | NOT_APPLICABLE | Server: `WR_AI_PROVIDER`, `WR_AI_MODEL`, `WR_AI_KEY_FILE` | test_rights | keine | 0 |
| D01 | Import | Ordner rekursiv vom lokalen Dateisystem | FULL (GUI) | NOT_APPLICABLE | `discover_documents(folder)` | test_importer | keine | 0 |
| D02 | Import | Persistente Importaufträge mit Fortschritt, Abbruch, Fortsetzen | FULL (GUI) | MISSING | `ImportJobs`, Tabellen `import_jobs`, `import_items` | keine Web-Tests | P1 | 4 bis 6 |
| D03 | Import | Importbericht und Fehlerjournal | FULL (GUI) | MISSING | `import_item_errors`, `write_report` | keine Web-Tests | P1 | 1 bis 2 |
| D04 | Import | OCR für Bilder und Scans | FULL (GUI, optional) | MISSING | `ocr.py`, Server lehnt Bilder ab | test_web (Ablehnung) | P2 | 3 bis 5 |
| D05 | Import | OCR-Quarantäne erneut verarbeiten | FULL (GUI) | MISSING | `retry_quarantined` | keine Web-Tests | P3 | 1 |
| D06 | Import | PST-Import mit Vorschau | FULL (GUI, optional) | MISSING | `import_pst`, `pst_archive.py` | keine Web-Tests | P3 | 3 bis 5 |
| D07 | Import | IMAP-Import mit Vorschau und Anhangsfreigabe | FULL (GUI) | MISSING | `preview_imap`, `import_imap_ids` | keine Web-Tests | P3 | 4 bis 7 |
| D08 | Import | Microsoft-Graph-Mailimport | FULL (GUI) | MISSING | `begin_microsoft`, `import_microsoft_ids` | keine Web-Tests | P3 | 3 bis 5 |
| D09 | Import | Postfachsynchronisation im Intervall | FULL (GUI) | MISSING | `mail_sync.MailSync` | keine Web-Tests | P3 | 2 bis 3 |
| D10 | Import | E-Mail-Konten verwalten | FULL (GUI) | MISSING | `MailAccounts`, Geheimnis im OS-Schlüsselspeicher | keine Web-Tests | P3 | 2 bis 4 |
| D11 | Import | Regeln für Bereich und Tags | FULL (GUI, CLI) | PARTIAL | Regeln wirken in `_insert_document`, keine Verwaltung | keine Web-Tests | P2 | 2 bis 3 |
| D12 | Import | Anhänge alter E-Mails entfernen | FULL (GUI) | MISSING | `mail_cleanup` | keine Web-Tests | P3 | 1 |
| D13 | Import | Chunk-Strategie je Dokumenttyp | FULL (CLI) | PARTIAL | Standardwerte wirken, keine Verwaltung | keine Web-Tests | P3 | 0,5 |
| E01 | Sicherheit | Geheimnisse im OS-Schlüsselspeicher | FULL | NOT_APPLICABLE | Server: Docker-Secrets über `_secret()` | test_server | keine | 0 |
| E02 | Datenschutz | Datenschutzexport aller Daten | FULL (GUI, CLI) | MISSING | `privacy.py` | keine Web-Tests | P2 | 1,5 bis 2,5 |
| E03 | Datenschutz | Selektive Löschung mit allem Abgeleiteten | FULL (GUI, CLI) | MISSING | `privacy.erase`, Audit `privacy_erase` | keine Web-Tests | P1 | 1,5 bis 2,5 |
| E04 | Datenschutz | Aufbewahrungsregeln | FULL (GUI, CLI) | MISSING | `retention.py` | keine Web-Tests | P2 | 1 bis 2 |
| E05 | Betrieb | Strukturiertes Protokoll mit Geheimnismaskierung | FULL | FULL | `local_logging`, Uvicorn-Zugriffsprotokoll nur mit Pfad (`server.PathOnlyAccessLog`, Phase 11.0) | test_server AccessLogTests | erledigt, Einsatz auf VPS offen | 0 |
| F01 | Wissensstruktur | Sammlungen anzeigen und exportieren | FULL (GUI, CLI) | FULL | `collections`, `export_collection`, `GET /api/collections/{id}/export` | test_web, test_rights, test_audit | erledigt | 0 |
| F02 | Wissensstruktur | Sammlung anlegen und löschen | FULL (GUI, CLI) | MISSING | `create_collection`, `delete_collection` | keine Web-Tests | P1 | 1 bis 1,5 |
| F03 | Wissensstruktur | Sammlung umbenennen, Dokumente zuordnen | FULL (GUI, CLI) | PARTIAL | `PATCH /api/collections/{id}`, keine Seite | test_concurrency t2 | P2 | 1 bis 1,5 |
| F04 | Wissensstruktur | Dokumentbeziehungen anzeigen | FULL (GUI, CLI) | PARTIAL | `relations` nur im JSON | test_rights | P2 | 0,5 |
| F05 | Wissensstruktur | Dokumentbeziehungen anlegen und entfernen | FULL (GUI, CLI) | MISSING | `relate`, `unrelate` | keine Web-Tests | P2 | 1 bis 1,5 |
| F06 | Wissensgraph | Entitäten und Relationen mit Beleg anlegen | FULL (GUI, CLI) | MISSING | `create_entity`, `relate_entities` | keine Web-Tests | P2 | 2 bis 3 |
| F07 | Wissensgraph | Entität umbenennen | FULL (GUI, CLI) | PARTIAL | `PATCH /api/entities/{id}`, keine Seite | test_concurrency | P3 | 0,5 |
| F08 | Wissensgraph | Zusammenführen, Trennen, Löschen, Zurücknehmen | FULL (GUI, CLI) | MISSING | `merge_entities`, `split_entity`, `revert_graph_change` | keine Web-Tests | P3 | 3 bis 4 |
| F09 | Wissensgraph | KI-Vorschläge prüfen und bestätigen | FULL (GUI, CLI) | MISSING | `graph_suggest.py`, `confirm_graph_item` | keine Web-Tests | P3 | 2 bis 3 |
| F10 | Wissensgraph | Graphansicht | FULL (GUI) | PARTIAL | `GET /api/graph` liefert JSON, keine Darstellung | test_rights | P3 | 2 bis 4 |
| F11 | Wissensgraph | Entitäts- und Relationsarten verwalten | FULL (CLI) | MISSING | `add_entity_type`, `add_relation_type` | keine Web-Tests | P3 | 1 |
| G01 | Administration | Betriebsübersicht | FULL (GUI, CLI) | MISSING | `operations.overview` | keine Web-Tests | P2 | 1 bis 1,5 |
| G02 | Administration | Integritätsprüfung | FULL (GUI, CLI) | MISSING | `backups.integrity` (SQLite-lastig) | keine Web-Tests | P2 | 1 bis 2 |
| G03 | Administration | Sicherung und Wiederherstellungsübung | FULL (GUI, CLI, SQLite) | PARTIAL | `deploy/backup.sh`, `backup-pull.sh`, `restore-drill.sh`, `server.audit_check` (Phase 11.0) | test_audit ServerAuditCheckTests, manuelle Probe PG 16 | P0 | 0,5 (Einrichtung, Erstlauf) |
| G04 | Administration | Geführte Wiederherstellung | FULL (GUI, SQLite) | NOT_APPLICABLE | Betriebsprozess statt Web-Funktion | test_backups | keine | 0 |
| G05 | Administration | Datenbankprofil, Verbindungstest, Schema-Reset | FULL (GUI) | NOT_APPLICABLE | Server: Umgebung und Migrationsdienst | test_database | keine | 0 |
| G06 | Administration | Konfigurationstransfer | FULL (GUI) | NOT_APPLICABLE | `configuration.py` | test_configuration | keine | 0 |
| G07 | Administration | Startdiagnose | FULL (GUI, CLI) | PARTIAL | `/healthz`, `check_postgres_current` | test_rights | P2 | 1 |
| G08 | Administration | Semantische Suche aktivieren, Vektorstand | FULL (GUI) | MISSING | `VectorSync`, kein Hintergrundlauf im Server | keine Web-Tests | P1 | in B02 |
| H01 | Oberfläche | Hauptnavigation | FULL | FULL | `base.html` | test_web | erledigt | 0 |
| H02 | Oberfläche | Hell- und Dunkelmodus | FULL | MISSING | `design.py` | keine | P3 | 0,5 bis 1 |
| H03 | Oberfläche | Logo und Branding | FULL | MISSING | `design.py` | keine | P3 | 0,5 |
| H04 | Oberfläche | Fortschritts- und Statusanzeigen | FULL | PARTIAL | kein htmx-Indikator, keine Statuszeile | keine | P2 | 0,5 bis 1 |
| H05 | Oberfläche | Verständliche Fehlermeldungen | FULL | PARTIAL | HTTP-Fehler gut, unbehandelte Ausnahmen als 500 | test_web | P1 | in C08 |
| H06 | Oberfläche | Tastaturbedienung und Barrierefreiheit | teilweise (Enter-Bindungen) | UNKNOWN | keine Prüfung | keine | P3 | 1 bis 2 |

## 6. Nachweise und fehlende Bestandteile

| ID | Nachweis im Code | Fehlt für FULL |
|---|---|---|
| A01 | `server.create_document` mit Rolle admin/editor, Besitzerfreigabe, Audit `document_created` | Seite in `web.py` |
| A02 | `web.upload`: Größenprüfung, `extract_document(allow_ocr=False)`, Besitzerfreigabe, Audit | nichts für Textformate. Grenze siehe Architektur-Review (Body-Limit) |
| A03 | `app.show_import_preview`, `ImportJobs.preview_files` | Mehrfach-Upload, Vorschau, Auswahl |
| A05, A06 | `web.metadata` übernimmt nur `title` und `category` | Formularfelder, Validierung über vorhandenes `update_metadata`, Tests |
| A07 | `rules.history`, Tabelle `metadata_changes` | Seite, Rechteprüfung |
| A09 | `Store.delete`, `audit.EVENTS` enthält `document_deleted` | Endpunkt, Regel wer löschen darf (Besitzer, Admin), Audit, Test I3 und E1 |
| A11 | `Store.add` liefert bei gleichem `sha256` die vorhandene Kennung. Server antwortet 409 ohne Freigabe | Entscheidung zur Dublette über Kontogrenzen (siehe Review S-05) |
| A12 | Upload ruft `store.add` ohne `source` und ohne `versioned` | Quellenkennung im Web definieren |
| A14, B08 | Tabellen `favorites` und `saved_searches` haben keine Spalte `account_id` | Migration mit Konto, Rechteprüfung |
| B02, G08 | `EMBEDDING_PROVIDERS['ollama']['endpoint']` fest auf `http://127.0.0.1:11434/api/embed`. `WR_OLLAMA_URL` gilt nur für `/api/chat`. Kein Hintergrundlauf von `VectorSync` im Server. | konfigurierbarer Embedding-Endpunkt, Worker für Einbettung, Aktivierung durch Admin, Test |
| C02 | `AnswerResult.assessment` existiert, `answer.html` zeigt es nicht | Ausgabe des Status |
| C08 | `AI.generate` wirft `ProviderError` (auch bei Budgetgrenze, `budgets.py`). `web.ask` und `server.question` fangen nur `HTTPException`. Ergebnis: 500 ohne Audit-Eintrag. Codebefund, nicht durch Test reproduziert. | Fehlerbehandlung, 503 mit Meldung, Test |
| C09 | `deploy/wissensraum.env.beispiel` nennt `qwen3:8b`, Betrieb laut Auftrag `qwen3:4b`. Kontextgrenzen `OLLAMA_*_CONTEXT` sind auf `qwen3:8b` gemessen | Messung mit eingefrorenem Bestand V2 oder Rückkehr zu `qwen3:8b` |
| D02, D03 | Importaufträge laufen im Desktop in einem Thread der Oberfläche | Hintergrundarbeiter im Server, Statusseite, Rechte je Auftrag |
| D04 | `web.upload` setzt `allow_ocr=False`, Image ohne Tesseract | Worker-Container mit Tesseract, Limits |
| D06 bis D10, D12 | Postfachzugänge liegen im OS-Schlüsselspeicher (`settings.SystemVault`), im Container nicht vorhanden | Entscheidung zur Geheimnisablage je Konto, Mail.Read je Benutzer |
| E05 | `uvicorn.run` ohne `access_log=False`. Uvicorn schreibt Pfad mit Querystring, also `/auth/callback?code=...&state=...`, in stdout, Docker speichert es per `json-file` | Zugriffsprotokoll abschalten oder Query für `/auth/callback` schwärzen, Traefik-Accesslog prüfen |
| F02 | Kein Endpunkt für `create_collection` und `delete_collection`. Ein Bearbeiter kann damit keine Sammlung anlegen und Freigaben je Sammlung nicht nutzen | Endpunkte, Besitzerfreigabe für Ersteller, Audit |
| F10 | `graph_view.py` zeichnet auf Tk-Canvas | serverseitiges SVG oder Liste |
| G03 | `backups.py` arbeitet nur mit SQLite. `BETRIEB.md` Abschnitt „Offen“ nennt `pg_dump` als offen | Sicherungsjob außerhalb des VPS, Wiederherstellungsübung |

## 7. Funktionen nur im Web

| ID | Funktion | Nachweis | Tests |
|---|---|---|---|
| W01 | Anmeldung über Entra ID (OIDC, PKCE, `nonce`, `tid`, Browserbindung) | `oidc.py`, `server.login`, `server.callback` | test_server S1 |
| W02 | Konten anlegen, sperren, Rolle ändern | `accounts.py`, `/verwaltung/konten` | test_web, test_accounts |
| W03 | Rollen admin, editor, reader | `accounts.ROLES`, `require_role`, `can_write` | test_rights e1 |
| W04 | Freigaben je Dokument und Sammlung | `accounts.grant`, `visible_documents` | test_rights, test_web |
| W05 | Serverseitige Sitzungen (30 min, 8 h, Hash) | `sessions.py` | test_server S2 |
| W06 | CSRF-Token, Origin-Prüfung, Sicherheits-Header, Hostprüfung | `server.protect` | test_server E2 |
| W07 | Hash-verketteter Audit-Verlauf | `audit.py`, Trigger | test_audit T1, R1 |
| W08 | Revisionen mit 409 | `core._revise` | test_concurrency T2 |
| W09 | Sperre nach fehlgeschlagenen Anmeldungen | `RateLimit` | test_server S3 |
| W10 | Verwaltungsseite für Konten und Freigaben | `web.admin_*` | test_web |
| W11 | Healthcheck | `/healthz` | test_rights |
| W12 | Laufzeitrolle ohne DDL, Audit nur Einfügen | `grant_runtime_role` | test_postgres (bestanden auf PG 16) |
| W13 | Responsives Layout | `meta viewport` vorhanden, `style.css` ohne Media Queries | keine |

## 8. Abgleich Masterplan und tatsächlicher Stand

`masterplan.md` wurde nicht geändert.

| Nr. | Aussage im Plan | Befund | Bewertung |
|---|---|---|---|
| M1 | Kopf: Dokumentversion 1.9, Stand 07.11.2026, Produktstand 2.1 | Inhalt reicht bis 09.11.2026 und Release 2.4 | veraltet |
| M2 | Phase 10.3 ✅ mit „Rate Limits“, `docs/sicherheit-3.0.md`: Rate Limits für Anmeldung, Suche, KI-Fragen und Uploads | nur Anmeldung begrenzt (`FAILED_SIGN_INS`, `SIGN_IN_STARTS`) | widersprüchlich |
| M3 | Gate-Protokolle für 10.2 bis 10.7 (`local-ci-gate-1ad320f.md`, `27a480a`, `660a7f0`, `0500631`, `91c4d3e`, `ee0a6ef`) und 8B (`bb8d1fe`) | Dateien nicht im Repository. Im Repo liegen nur ältere Gate-Dateien | im Repository unbelegt |
| M4 | 10.7: Testzahl Linux 725 | lokaler Lauf: 728 Tests (Python 3.12) | plausibel, Abweichung durch spätere Commits |
| M5 | `docs/sicherheit-3.0.md` Abschnitt 4: Ablaufwarnung für das Entra-Client-Geheimnis in der Betriebsübersicht | nicht implementiert, in `BETRIEB.md` als offen geführt | offen, korrekt als offen dokumentiert |
| M6 | `docs/sicherheit-3.0.md` Audit erfasst Löschung, Datenschutzlöschung, Einstellungen | Ereignisse definiert, im Server keine Endpunkte | Plan vor Ist |
| M7 | Arbeitsprogramm P0: veröffentlichtes Datenbankkennwort ändern, „spätestens vor der ersten extern erreichbaren Instanz“ | Laut Product Owner am 10.11.2026 rotiert, Datum der Rotation noch nachzutragen (`deploy/BETRIEB.md`) | erledigt laut Angabe, im Masterplan nachziehen |
| M8 | `BETRIEB.md`: Rocky Linux 9, Modell `qwen3:8b` | Auftrag: Rocky Linux 10, `qwen3:4b` | widersprüchlich |
| M9 | Pflichtenheft 7.2: pgvector | D-016 verschoben, Byte-Spalte mit numpy | korrekt dokumentiert, Auftrag nimmt anderes an |
| M10 | 10.8 🟡 offen: VPS-Einrichtung, Sicherung, Probelauf mit zwei Konten | Einrichtung und Admin-Anmeldung laut Auftrag erfolgt. Sicherung und Zwei-Konten-Probelauf nicht belegt | Status 🟡 weiterhin zutreffend |
| M11 | Phase 11.3 verlangt „vereinbarte Funktionsparität“ | Parität 15,9 %, Zielumfang nicht vereinbart | Vereinbarung fehlt |
