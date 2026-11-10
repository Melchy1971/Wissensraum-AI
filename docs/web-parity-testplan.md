# Wissensraum-AI: Teststrategie und Akzeptanzkriterien für die Web-Parität

Stand: 10.11.2026, Commit `bf3b126`. Bezug: `docs/desktop-web-parity.md`, `docs/web-parity-roadmap.md`, `docs/web-architecture-review.md`.

## 1. Ausgangslage (gemessen am 10.11.2026)

| Lauf | Umgebung | Ergebnis |
|---|---|---|
| Gesamtsuite | Linux, Python 3.12, Tk, Xvfb | 728 Tests, 1 Fehlschlag (GUI, 1 Pixel), 59 übersprungen |
| Gesamtsuite | Linux, Python 3.13 ohne Tk | 609 Tests, 3 Module nicht ladbar, 60 übersprungen. Nicht verwertbar. |
| PostgreSQL-Vertrag | PostgreSQL 16, Wegwerf-Cluster | 49 von 49 |
| Server und Web | `test_server`, `test_web`, `test_rights`, `test_audit`, `test_concurrency`, `test_accounts` | 45 von 45 |

Übersprungen: 46 PostgreSQL ohne Konfiguration (im Gesamtlauf), 10 Tesseract fehlt, 3 nur Windows. Nicht ausgeführt: Matrix-Gate, Windows, PostgreSQL 18, Browser mit echter Entra-Anmeldung, Last, Antwortqualität mit `qwen3:4b`.

Der GUI-Fehlschlag `test_settings_pages_scroll_to_their_last_widget_at_minimum_size (page='mail')` ist vor Beginn der Arbeiten im Matrix-Gate zu prüfen. Ist er dort grün, als Umgebungseffekt dokumentieren.

## 2. Teststufen

| Stufe | Zweck | Werkzeug | Läuft |
|---|---|---|---|
| Vertrag | gleicher Speichervertrag für SQLite und PostgreSQL | `unittest`, `check_*_contract` | jeder PR, beide Backends |
| Endpunkt | Rechte, Rollen, CSRF, Fehlerfälle je Route | `fastapi.testclient`, lokaler Test-Identitätsanbieter aus `test_server` | jeder PR |
| Ende-zu-Ende Server | Seitenablauf mit zwei Konten | `test_web` erweitern | jeder PR |
| Worker | Aufträge, Wettlauf, Neustart | `unittest` mit echtem PostgreSQL | jeder PR mit Worker-Änderung |
| Browser | echte Anmeldung, htmx, Layout | manuell nach Protokoll, später Playwright | je Phase, vor Abnahme |
| Qualität | Antwortgüte des eingesetzten Modells | `tests/eval/evaluate.py` auf Bestand V2 | bei Modell- oder Retrieval-Änderung |
| Leistung | Antwortzeiten | `tests/bench/benchmark.py`, Messung auf dem VPS | Phase 2, danach je Release |
| Sicherheit | Secret-Scan, Abhängigkeiten, statische Prüfung | `security.yml` (gitleaks, pip-audit, bandit) | jeder PR, wöchentlich |

## 3. Pflichtfälle je neuer Web-Funktion

Jede neue Route bekommt mindestens:

| Kennung | Fall | Erwartung |
|---|---|---|
| I1 | zweites Konto ohne Freigabe liest Liste, Suche, Detail | Objekt erscheint nicht, Zählung und Ränge unverändert |
| I3 | fremde und nicht vorhandene Kennung | gleiche Antwort 404, gleicher Text |
| E1 | jede Rolle ruft die Route auf | reader schreibt nie, editor nur mit Schreibrecht, Admin-Routen nur admin |
| E2 | ohne CSRF-Token, falsches Token, fremder Origin | 403, nichts geändert |
| T2 | zwei Bearbeiter mit derselben Revision | genau einer gewinnt, der andere bekommt 409 und den aktuellen Stand |
| R1 | Änderung erfolgreich | genau ein Audit-Eintrag, ohne Texte, Kette gültig |
| R2 | Audit-Schreiben schlägt fehl (Fehlerinjektion) | Fachänderung zurückgerollt (ab Phase 1) |
| F1 | Fachfehler (Ollama weg, Budget, Datenbank) | verständliche Meldung, kein 500, keine internen Details |
| V1 | Eingabegrenzen | Werte an der Grenze angenommen, darüber 422 |

## 4. Zusätzliche Fälle je Phase

| Phase | Fall | Akzeptanzkriterium |
|---|---|---|
| 0 | Rückruf `/auth/callback?code=X&state=Y` ausführen und Logausgabe erfassen | Ausgabe enthält weder `X` noch `code=` |
| 0 | `git check-ignore` für `deploy/secrets/db_app.txt` und `deploy/wissensraum.env` | beide ignoriert |
| 0 | Rückspielprobe der Sicherung | gleiche Zeilenzahl je Tabelle, `audit-verify` liefert `ok: true` und denselben Kopf-Hash |
| 1 | Fehler in der Besitzerfreigabe nach `store.add` | kein Dokument ohne Besitzer in der Datenbank |
| 1 | Rate Limit Fragen, Upload, Suche | Anfrage über Grenze ergibt 429, Zähler je Konto |
| 1 | `ProviderError` und Budgetgrenze | 503 mit Meldung, Audit `ai_question` mit Status `unavailable` |
| 2 | Embedding-Endpunkt aus `WR_OLLAMA_URL` | Fake-Server erhält Anfrage auf `/api/embed` |
| 2 | Vektorsuche über den Server mit unsichtbarem Spitzentreffer | Treffer fehlt, Ränge der sichtbaren Treffer wie ohne das Dokument |
| 2 | Antwortqualität mit eingesetztem Modell | False Accept und False Reject innerhalb der Grenzen von Release 2.1, sonst Modellwechsel |
| 2 | Antwortzeiten auf dem VPS | Suche p95 und Frage p95 dokumentiert, Zielwerte vom Product Owner festgelegt |
| 3 | zwei Worker holen denselben Auftrag | genau einer bearbeitet ihn |
| 3 | Worker-Neustart mitten im Auftrag | Auftrag setzt fort, keine Dublette |
| 3 | ZIP mit Dekompressionsbombe | abgelehnt, Fehlerjournal |
| 3 | fremder Importauftrag | 404 |
| 3 | chunked Upload über der Grenze | 413 ohne vollständiges Einlesen |
| 4 | Löschen durch Besitzer, durch fremdes Konto, durch Admin | erlaubt, 404, erlaubt. Audit `document_deleted` |
| 4 | Favoriten und gespeicherte Suchen je Konto | Konto B sieht Favoriten von A nicht |
| 4 | Migration 25 mit Bestand | bestehende Favoriten gehören dem ersten Administrator, Rückwärtsmigration mit Sicherung |
| 5 | Einstellungsänderung | nur Admin, Audit `settings_changed` |
| 5 | Betriebsübersicht | keine Titel, Texte, Fragen oder Pfade in der Antwort |
| 6 | OCR-Tests im Worker-Image | 10 bisher übersprungene Tests laufen und bestehen |
| 7 | Graph mit Beleg in unsichtbarem Dokument | Entität nicht sichtbar, Beleg nie angezeigt |
| 8 | Postfachzugang | Geheimnis weder in Datenbank im Klartext noch in Log, Mail.Read als einzige Berechtigung |
| 9 | Layout bei 375 px Breite | keine horizontale Rolle, alle Aktionen erreichbar |

## 5. Definition von FULL in der Matrix

Eine Zeile wechselt nur auf FULL, wenn gleichzeitig gilt:

1. Route oder Seite vorhanden und aus der Navigation erreichbar.
2. Daten werden in PostgreSQL gespeichert und im Vertragstest für beide Backends geprüft, soweit der Kern betroffen ist.
3. Fälle I1, I3, E1 und, wenn schreibend, E2, T2, R1 sind als Tests vorhanden und grün.
4. Fehlerfälle F1 und V1 sind getestet.
5. Matrix-Gate 7 von 7 am Commit und grüne CI.
6. Der Test ist in der Spalte „Tests“ der Matrix namentlich eingetragen.

Ein übersprungener Test zählt nicht als bestanden.

## 6. Manuelles Browser-Protokoll je Abnahme

Mit zwei echten Entra-Konten (admin, editor) und einem Leserkonto, über VPN:

1. Ohne VPN: 403 von Traefik.
2. Anmeldung, Abmeldung, erneute Anmeldung. Sitzung nach 30 Minuten Inaktivität beendet.
3. Editor lädt Dokument hoch, Leser sieht es nicht. Admin gibt frei, Leser sieht es.
4. Editor und Admin ändern dieselben Metadaten, einer erhält den Konflikthinweis.
5. Frage mit Quelle aus freigegebenem Dokument, Frage nach Inhalt eines nicht freigegebenen Dokuments ohne Quelle daraus.
6. Original-Download, Sammlungsexport.
7. Konto sperren, gesperrtes Konto wird bei der nächsten Anfrage abgemeldet.
8. `audit-verify` nach dem Durchlauf, Anzahl und Kopf-Hash notieren.
9. `docker logs` des Dienstes `server` auf `code=` und Kennwörter durchsuchen.

Ergebnis mit Datum, Commit und Image-Digest im Masterplan protokollieren.
