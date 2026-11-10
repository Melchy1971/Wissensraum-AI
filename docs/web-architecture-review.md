# Wissensraum-AI: Architektur- und Sicherheitsbewertung der Webanwendung

Stand: 10.11.2026, Commit `bf3b126`. Analysephase, keine Codeänderung. Grundlage und Testläufe siehe `docs/desktop-web-parity.md` Abschnitt 1.

## 1. Gesamtbild

Der Server ist kein Nachbau der Desktop-Anwendung. Er nutzt denselben Fachkern (`core.py`, `retrieval.py`, `evidence.py`, `rules.py`, `migrations.py`) und legt Rechte, Sitzungen und Audit darüber. Das ist die richtige Grundentscheidung. Der Sicherheitskern (Anmeldung, Sitzungen, CSRF, Rechtefilter in SQL, Audit-Kette, Revisionen) ist sorgfältig gebaut und mit Negativtests belegt.

Die Lücken liegen an drei Stellen:

1. Betrieb: Zugriffsprotokoll, Secret-Ablage im Arbeitsbaum, fehlende Sicherung.
2. Desktop-Annahmen im Kern: fester Ollama-Endpunkt für Einbettungen, globale Einstellungen, globale Favoriten und gespeicherte Suchen, Hintergrundarbeit im Tk-Thread.
3. Fehlende Service-Schicht: Rechte, Transaktion und Audit sind je Endpunkt von Hand verdrahtet und zwischen `server.py` und `web.py` doppelt.

## 2. Sicherheitsbefunde

Stand Phase 11.0 (Branch `phase-11-0-betrieb`, 10.11.2026): S-01 und S-02 im Code behoben, S-03 mit Skripten umgesetzt (Erstlauf auf VPS und Unraid offen), S-04 laut Product Owner erledigt. S-05 bleibt bewusst so (Entscheidung Product Owner 10.11.2026), siehe Abschnitt 7.

| Nr. | Prio | Befund | Nachweis | Wirkung | Maßnahme | Aufwand |
|---|---|---|---|---|---|---|
| S-01 | P0 | OAuth-Autorisierungscode im Zugriffsprotokoll | `server.main` ruft `uvicorn.run` ohne `access_log=False`. Uvicorn protokolliert `get_path_with_query_string`, also `GET /auth/callback?code=...&state=...`. Docker hält das per `json-file` vor (`deploy/compose.yaml`). | Code ist einmalig, an PKCE und Client-Geheimnis gebunden. Ausnutzbarkeit gering, Verstoß gegen die eigene Regel „keine Tokens in Logs“. Gleiches gilt für ein eventuell aktives Traefik-Accesslog (nicht geprüft). | `access_log=False` oder Log-Filter, der die Query von `/auth/callback` entfernt. Traefik `accessLog.fields.names` bzw. Query-Filter prüfen. Vorhandene Docker-Logs rotieren. Test: Log-Ausgabe einer Rückrufanfrage enthält keinen `code`. | 0,5 PT |
| S-02 | P0 | Secrets und Umgebungsdatei nicht von Git ausgeschlossen | `.gitignore` deckt `.env*` (nur Dateien mit Punkt am Anfang), `*.pem`, `*.key`. Nicht gedeckt: `deploy/secrets/*.txt`, `deploy/wissensraum.env`. Mit `git status` auf einer Kopie nachgewiesen. Das Repository ist öffentlich. | Ein `git add -A` auf dem VPS veröffentlicht DB-Kennwörter und das Entra-Client-Geheimnis. | `deploy/secrets/` und `deploy/wissensraum.env` in `.gitignore`. Auf dem VPS `git status --ignored` prüfen. gitleaks-Lauf über den VPS-Arbeitsbaum. | 0,25 PT |
| S-03 | P0 | Keine Sicherung der Server-Datenbank | `backups.py` arbeitet nur mit SQLite. `BETRIEB.md` führt `pg_dump` und Wiederherstellungsübung als offen. | Datenverlust bei Plattenfehler oder Fehlbedienung. Audit-Kette ohne Sicherung nicht rekonstruierbar. | Täglicher `pg_dump -Fc` mit Eigentümerrolle, verschlüsselt außerhalb des VPS, Aufbewahrung, vierteljährliche Rückspielprobe in eine Wegwerfdatenbank mit `audit-verify`. | 2 bis 3 PT |
| S-04 | P0 | Offener P0 aus dem Masterplan: veröffentlichtes Datenbankkennwort | Masterplan Abschnitt 8 und 10.1, nicht abgehakt. VPS ist in Betrieb. | Wenn das alte Kennwort für einen Superuser oder für dieselbe Instanz gilt, ist die neue Server-DB mit betroffen. | Rotation nachweisen (Datum, betroffene Rollen), `pg_hba.conf` prüfen. Nicht aus dem Code belegbar. | 0,5 PT |
| S-05 | P1 | Dublettenprüfung über Rechtegrenzen | `Store.add` prüft `sha256` über alle Dokumente. Der Server antwortet 409, ohne dem Hochladenden Zugriff zu geben (`server.py` Zeile 397 mit `ponytail`-Hinweis). | Ein Bearbeiter erfährt, dass ein bestimmter Text existiert, und kann sein eigenes Exemplar nicht ablegen. Datenleck durch Raten ist auf exakten Volltext beschränkt. | Entscheidung des Product Owners: (a) Dublette nur innerhalb der sichtbaren Dokumente prüfen und Kopie zulassen, oder (b) bei Dublette Lesefreigabe für das vorhandene Dokument erteilen und auditieren. (a) widerspricht der Kennzahl „0 exakte Dubletten“, (b) gibt Zugriff ohne Freigabe. | 2 bis 4 PT |
| S-06 | P1 | Rate Limits nur für Anmeldung | `docs/sicherheit-3.0.md` 4 und STRIDE D1 verlangen Limits für Suche, KI-Fragen und Uploads. Im Code nur `FAILED_SIGN_INS`, `SIGN_IN_STARTS`. | Ein Konto kann Ollama und die CPU dauerhaft belegen. Im VPN mit wenigen Konten geringes Risiko, aber Gate-Widerspruch. | `RateLimit` je Konto für `/fragen`, `/api/questions`, `/hochladen`, `/suche`. Test je Grenze. | 1 PT |
| S-07 | P1 | Änderung und Audit nicht atomar | `audit.record` öffnet eine eigene Transaktion. `create_document`, `upload`, `change_document` schreiben vorher und committen. `upload` erzeugt Dokument und Besitzerfreigabe in zwei Transaktionen. | Fällt der Audit-Eintrag aus, ist die Änderung trotzdem gespeichert (Lücke im Audit). Fällt die Freigabe aus, liegt ein Dokument ohne Besitzer vor, nur für Admins sichtbar. | Service-Funktion, die Änderung, Freigabe und Audit in einer Datenbanktransaktion ausführt. `audit.record` mit übergebener Verbindung. | 1,5 bis 2 PT |
| S-08 | P1 | Unbehandelte Fachfehler ergeben 500 | `ProviderError` aus `AI.generate` (Ollama nicht erreichbar, HTTP-Fehler, Budget erschöpft) wird in `web.ask` und `server.question` nicht gefangen. Codebefund, nicht per Test reproduziert. | Benutzer sieht „Internal Server Error“. Kein Audit-Eintrag für die Frage. | `ProviderError` in 503 mit Meldung übersetzen, Audit mit Status `unavailable`. | 0,5 bis 1 PT |
| S-09 | P2 | Upload-Grenze greift nur mit `Content-Length` | `web.upload` prüft den Header, `request.form()` liest danach den ganzen Body. Bei `Transfer-Encoding: chunked` fehlt die frühe Grenze. Temporäre Dateien liegen auf `tmpfs /tmp`, das zum Speicherlimit von 1 GB zählt. | Ein angemeldeter Bearbeiter kann den Container in Speichermangel treiben. | Traefik-Middleware `buffering.maxRequestBodyBytes` (z. B. 55 MB) und ASGI-Grenze. Test mit chunked Upload. | 0,5 bis 1 PT |
| S-10 | P2 | KI-Verlauf behält Zitate nach Entzug einer Freigabe | `AI._record` speichert zitierte Abschnitte im Verlauf des Kontos. `Store.history` filtert nicht nach aktueller Sichtbarkeit. | Nach Entzug bleibt zitierter Text im eigenen Verlauf lesbar. | Bewusst entscheiden: dokumentieren, oder beim Entzug Verlaufseinträge mit Quellen des Dokuments schwärzen. | 1 PT |
| S-11 | P2 | Ablaufwarnung Entra-Client-Geheimnis fehlt | Vorgabe in `docs/sicherheit-3.0.md` 4. | Anmeldung fällt bei Ablauf ohne Vorwarnung aus. | Ablaufdatum als Umgebungswert, Warnung im Log und auf der Verwaltungsseite ab 30 Tagen. | 0,5 PT |
| S-12 | P3 | JWKS-Neuladen bei jedem unbekannten `kid` | `oidc.Provider._key` lädt bei unbekanntem Schlüssel neu. Erst nach erfolgreichem Code-Tausch erreichbar. | Geringe Last auf Entra bei manipulierten Tokens. | Mindestabstand zwischen Neuladungen. | 0,25 PT |

Geprüft und ohne Befund: PKCE S256, `state` einmalig und browsergebunden, `nonce` gehasht, `tid` und `oid` statt E-Mail, nur RS256, Sitzungs-ID nur als Hash, Cookies `__Host-` mit `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF-Token mit `compare_digest`, Origin- und Referer-Prüfung, CSP ohne Inline-Skripte, htmx mit `allowEval=false`, `TrustedHostMiddleware`, keine OpenAPI-Seiten, Fehlercodes von `SignInError` ohne Tokeninhalt, Audit-Details nur Kennwerte, parametrisierte SQL-Werte, Rollen- und Rechteprüfung serverseitig je Endpunkt, Container ohne Root, `read_only`, `cap_drop: ALL`, `no-new-privileges`, getrennte DB-Rollen, Laufzeitrolle ohne DDL (Test auf PostgreSQL 16 bestanden).

Nicht prüfbar aus dem Repository: Traefik-Konfiguration und Accesslog, WireGuard-Peers, Firewall, `ssl=on` im PostgreSQL-Container, Inhalt von `deploy/secrets/`, Rotation der Kennwörter, Ablaufdatum des Client-Geheimnisses.

## 3. Architekturvergleich Desktop und Web

### 3.1 Gemeinsam genutzt

| Komponente | Desktop | Web | Bewertung |
|---|---|---|---|
| Speicher (`Store`, `PostgresStore`) | ohne Konto | `as_viewer(konto)` | gemeinsam, Rechte als SQL-Bedingung. Gut. |
| Suche (`Retriever`, RRF) | ja | ja | gemeinsam. Im Server wirkt nur der Volltextteil (siehe 3.3). |
| Antworten (`AI.ask`, `evidence`) | ja | ja | gemeinsam, gleiche Quellenprüfung. |
| Extraktion (`extract_document`) | ja | ja, ohne OCR | gemeinsam, gleiche Größen- und ZIP-Grenzen. |
| Regeln beim Einfügen | ja | ja | gemeinsam über `_insert_document`. |
| Validierung Titel, Bereich | `MAX_TITLE_LENGTH=300`, `MAX_CATEGORY_LENGTH=100` | Pydantic 300 und 100 | konsistent. |
| Migrationen | Schema 23 | Schema 23, Server prüft nur | konsistent. |

### 3.2 Doppelte Logik

| Stelle | Doppelung | Vorschlag |
|---|---|---|
| `server.create_document` und `web.upload` | Rolle, `store.add`, 409, Besitzerfreigabe, Audit | `services.create_document(konto, ...)` |
| `server.change_document` und `web.metadata` | Sichtbarkeit, `can_write`, `update_metadata`, Konflikt, Audit | `services.change_metadata(...)` |
| `server.question` und `web.ask` | KI-Aufruf, Audit, Quellenformat | `services.ask(...)` |
| `server.create_grant` und `web.admin_grant` | Freigabe und Audit. API prüft Revision, Webseite nicht | eine Funktion mit Revision |
| `Store` und `PostgresStore` | `add`, `list`, `get`, `search`, `log`, `history` je zweimal | bestehendes Muster der Mixins fortsetzen, nur SQL-Dialekt trennen |

### 3.3 Desktop-Annahmen im gemeinsamen Kern

| Annahme | Stelle | Folge im Server | Maßnahme |
|---|---|---|---|
| Ollama läuft auf demselben Rechner | `EMBEDDING_PROVIDERS['ollama']['endpoint'] = http://127.0.0.1:11434/api/embed` | Einbettung im Container unerreichbar, Suche fällt still auf Volltext zurück | Endpunkt aus Konfiguration, `WR_OLLAMA_URL` auch für `/api/embed` |
| Hintergrundarbeit läuft im Fenster-Timer | `VectorSync.run`, `MailSync.run_due`, `backups.auto`, `ImportJobs.run` werden aus `app.py` getaktet | im Server läuft nichts davon | eigener Worker-Prozess (gleiches Image, eigener Dienst in Compose) mit Auftragstabelle |
| Einstellungen gelten für die ganze Datenbank | Tabelle `settings` (Vektoren, `exclude_retired`, Budgets, Chunk-Strategien, Graphkontext) | jede Änderung wirkt für alle Konten | nur Admin darf ändern, Audit `settings_changed` |
| Ein Benutzer | `favorites`, `saved_searches` ohne `account_id` | würden zwischen Konten geteilt | Migration mit `account_id` |
| Geheimnisse im OS-Schlüsselspeicher | `settings.SystemVault`, `MailAccounts` | im Container nicht vorhanden | eigenes Konzept für Postfachzugänge je Konto (ADR) |
| Prozessweiter Vektor-Cache | `VECTOR_CACHE` | richtig bei einem Worker. Bei mehreren Prozessen mehrfacher Speicher | bei einem Uvicorn-Prozess bleiben oder pgvector erneut bewerten |
| Lokale Pfade | `import_document(path)`, `discover_documents(folder)`, Sicherungsordner, Logo | darf im Server nicht über Benutzereingaben erreichbar sein | nur Upload-Bytes annehmen. Kein Pfadparameter in Web-Endpunkten. Heute eingehalten. |

### 3.4 Unterschiedliche Fehlerbehandlung

Desktop: jede Aktion läuft über `App.work` und zeigt Fehler in einem Dialog. Web: `HTTPException` wird sauber als Seite oder JSON ausgegeben, andere Ausnahmen werden zu 500. Betroffen sind `ProviderError`, Budgetgrenze und Datenbankfehler außerhalb der abgefangenen Stellen.

### 3.5 Unterschiedliche Rechteprüfung

Alle Endpunkte prüfen serverseitig. Die Reihenfolge ist einheitlich „erst Sichtbarkeit (404), dann Rolle (403)“, außer `change_collection`, das nur `signed_in` und dann `can_write_collection` prüft. Ergebnis ist gleichwertig, weil `visible_collection` vorher 404 liefert. `change_document` lässt die Rolle `reader` in `require_role` zu und lehnt erst über `can_write` ab. Funktional korrekt, aber uneinheitlich.

### 3.6 Transaktionsgrenzen

Jeder `store.connect()`-Block ist eine Transaktion. Fachänderung, Freigabe und Audit liegen in getrennten Blöcken (S-07). `accounts.grant` beendet Sitzungen und ändert die Freigabe in einer Transaktion. Das ist korrekt.

### 3.7 Kandidaten für einen gemeinsamen Core

| Kandidat | Heute | Ziel |
|---|---|---|
| Anwendungsdienste mit Konto, Transaktion und Audit | in `server.py` und `web.py` verteilt | `services.py`, von API und Seiten genutzt. Desktop ruft mit Konto `None` |
| Hintergrundaufträge | Desktop-Thread | Auftragstabelle plus Worker, vom Desktop weiter direkt nutzbar |
| Einbettung | fest lokal | konfigurierbarer Adapter |
| Einstellungen | global | getrennt in Installations- und Kontoeinstellungen |

## 4. Technische Qualitätsprüfung

| Bereich | Befund | Bewertung |
|---|---|---|
| PostgreSQL-Kompatibilität | Vertrag 49 von 49 auf PostgreSQL 16 bestanden. PostgreSQL 18 nicht geprüft (Masterplan meldet 18.x grün). | gut, Version 18 lokal nicht nachvollzogen |
| Migrationen | Schema 23, Advisory Lock, Schemaprüfung ohne DDL beim Serverstart, Trigger gegen Audit-Änderung | gut |
| Zugriffskontrollen | Rechte als SQL-Bedingung vor Sortierung und Limit (`visible_documents`), Vektorsuche filtert Kandidaten vor dem Ranking | gut |
| Docker | unprivilegiert, nur lesbares Dateisystem, Healthcheck, Basis-Image nur per Tag gepinnt (`ponytail`-Hinweis im Dockerfile) | gut, Digest-Pin offen |
| Abhängigkeiten | fest gepinnt in `requirements*.txt`, `security.yml` mit pip-audit, gitleaks, bandit. Hinweis: Starlette meldet `httpx` im Testclient als veraltet | gut |
| API-Verträge | keine OpenAPI-Ausgabe (bewusst). Verträge nur durch Tests beschrieben | ausreichend für jetzt |
| Fehlerbehandlung | siehe 3.4, S-08 | Lücke |
| Logging | Betriebsprotokoll maskiert bekannte Geheimnisse. Uvicorn-Accesslog ungefiltert (S-01) | Lücke |
| Performance | `Store.list` lädt alle sichtbaren Zeilen und alle Tags, Seiten werden in Python geschnitten. Verwaltungsseite lädt alle Dokumente in eine Auswahlliste. Neuer `Retriever` je Anfrage, Fragevektor-Cache wirkt damit nicht. Keine Messung auf dem VPS. | bei einigen tausend Dokumenten spürbar, ungemessen |
| Testabdeckung | Server-Kern gut (S1 bis S3, E1, E2, I1 bis I3, T1, T2, R1). Keine Tests für Fehlerpfade der KI im Web, Log-Inhalt, chunked Upload, Rate Limits außer Anmeldung | gut mit Lücken |
| Backup und Restore | Server: keine (S-03) | Lücke |

## 5. Bewusst nicht 1:1 zu übertragen

| Funktion | Grund | Web-Ersatz |
|---|---|---|
| Ordnerimport vom Dateisystem | Server darf keine Pfade aus Benutzereingaben lesen | Mehrfach- oder ZIP-Upload mit Vorschau |
| Datenbankprofil, Verbindungstest, Schema-Reset | Betriebsaufgabe, Geheimnisse gehören nicht in die Oberfläche | Umgebung, Migrationsdienst |
| Geführte SQLite-Wiederherstellung | Server nutzt PostgreSQL | `pg_dump`/`pg_restore`-Ablauf |
| Konfigurationstransfer | Desktop-Profil | entfällt |
| KI-Schlüssel im UI | Docker-Secrets | Anzeige des aktiven Anbieters, Verbindungstest für Admin |
| OS-Schlüsselspeicher | im Container nicht vorhanden | ADR zur Ablage von Postfachzugängen |
| Graphkontext in Antworten | Gate verfehlt (D-025) | aus lassen |

## 6. Empfehlung zur Architektur

Erst Betrieb absichern (S-01 bis S-04), dann eine dünne Service-Schicht und einen Worker-Dienst einführen. Beides ist Voraussetzung für Import, OCR, Einbettung und E-Mail im Web. Ohne Worker bleibt jede lange Aufgabe eine blockierende HTTP-Anfrage. pgvector nicht vorziehen: Die exakte Suche mit numpy ist gemessen und bei einem Prozess ausreichend. Neu bewerten, sobald mehr als ein Serverprozess nötig wird.

## 7. Entscheidungen

| Datum | Thema | Entscheidung |
|---|---|---|
| 10.11.2026 | Sicherungsziel | Unraid holt über WireGuard ab, VPS hat keinen Zugang zu Unraid |
| 10.11.2026 | Verschlüsselung der Sicherung | age, privater Schlüssel nur beim Product Owner |
| 10.11.2026 | Veröffentlichtes DB-Kennwort | rotiert (Angabe Product Owner) |
| 10.11.2026 | Dublette über Kontogrenzen (S-05) | Verhalten bleibt: 409 ohne Zugriff. Restrisiko: ein Bearbeiter erfährt, dass ein exakt gleicher Text existiert. Kein Titel, keine Kennung wird preisgegeben. |
