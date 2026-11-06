# CLAUDE.md – Arbeitsanweisung für Wissensraum-AI

## 1. Rolle und Kommunikation

Du arbeitest als Senior Python Engineer für **Wissensraum-AI**.

Projektverantwortlicher ist Markus Dickscheit, Prozessdesigner und Product Owner. Kommuniziere auf Deutsch, sachlich, direkt und technisch präzise. Vermeide KI-Floskeln und unnötige Wiederholungen. Widersprich fachlich, wenn eine Anforderung Daten, Sicherheit, Kompatibilität oder Wartbarkeit gefährdet.

## 2. Projektziel

Wissensraum-AI ist eine lokale, deutschsprachige Desktop-Anwendung für persönliches Wissensmanagement. Sie importiert Dokumente, Notizen, PST-Dateien und ausgewählte E-Mails, speichert Inhalte wahlweise in SQLite oder PostgreSQL und beantwortet Fragen auf Basis gefundener Quellen.

Der Offline-Kern muss ohne KI-Anbieter funktionieren. Ein Webserver, Mehrbenutzerbetrieb, automatische Cloud-Synchronisation und ungefragte Datenübertragung gehören nicht zum aktuellen Produktumfang.

## 3. Verbindliche Quellen und Priorität

Lies vor jeder nicht-trivialen Änderung mindestens:

1. `masterplan.md` – verbindliche Phasen, Status, Test-Gates und Release-Freigaben
2. `README.md` – Bedienung, Produktgrenzen, Datenhaltung und unterstützte Konfiguration
3. die betroffenen Implementierungsdateien und Tests
4. `tests/local_ci.py` – verbindliches lokales Matrix-Gate (7 Jobs); `.github/workflows/tests.yml` beschreibt dieselbe Matrix und läuft seit 03.11.2026 wieder für Pull Requests und `main`

Bei Widersprüchen gilt der ausführbare Code zusammen mit reproduzierbaren Tests als Ist-Stand. Widersprüche zwischen Code, Tests, README und Masterplan müssen ausdrücklich benannt und im passenden Dokument korrigiert werden.

Statusangaben im Masterplan dürfen nur aufgrund nachprüfbarer Ergebnisse geändert werden. Vor jeder Arbeit den aktuellen Stand aus dem Repository lesen; keine Statuswerte aus dieser Datei als dauerhaft aktuell voraussetzen.

## 4. Aktueller Arbeitsschwerpunkt

Phasen 0 bis 7 und 8A sind abgenommen. Release 2.0, 2.0.1, 2.1 und 2.2 sind freigegeben, Release 2.1 am 05.11.2026 (Tag `v2.1`, D-021), Release 2.2 am 06.11.2026 (Tag `v2.2`, D-022). Den verbindlichen Stand je Phase, offene Gate-Kriterien und Entscheidungen enthält `masterplan.md` (Abschnitte „Aktualisierung“ am Anfang und Entscheidungstabelle).

Stand 06.11.2026:

- Phase 7 (Release 2.1 Hybrid-RAG): 7.1 bis 7.4, Chunk-Strategie je Dokumenttyp und Fundstellenanzeige ✅. Produktionsvariante der Quellenabsicherung ist B2 (Zweitabschnitt des besten Dokuments im Fenster von 9.216 Token, zweiter Beleglauf aus, D-020).
- Reihenfolge nach Masterplan (Entscheidung Product Owner 05.11.2026): Release 2.2 (Phase 8A Wissensorganisation), 2.3 (Phase 8B Wissensgraph), 2.4 (Phase 9 Betrieb und Governance), 3.0 (Phase 10 Mehrbenutzerbetrieb), danach 3.1 (VPS und Domain). 3.1 beginnt erst nach dem Sicherheits- und Mehrbenutzer-Gate von 3.0.
- Phase 8A (Release 2.2 Wissensorganisation) ✅: Metadaten mit Verlauf, Dokumentstatus und Ausschluss aus KI-Antworten, Sammlungen, Favoriten, gespeicherte Suchen, Beziehungen, Fassungsvergleich, Sammlungsexport mit Quellenmanifest. Datenbankschema 15.
- Phase 8B (Release 2.3 Wissensgraph) läuft nach dem freigegebenen Plan 8B.1 bis 8B.5 (Masterplan, D-023: Tabellen in SQLite/PostgreSQL, KI-Vorschläge nur lokales Ollama). 8B.1 Datenmodell (Schema 16) und 8B.2 KI-Vorschläge (Schema 17) umgesetzt. In Arbeit: 8B.3 Zusammenführen und Trennen, 8B.4 Graphansicht, danach 8B.5 Graphkontext für Antworten.

Messwerte der Antwortqualität stammen aus dem eingefrorenen Bestand V2 (`data/eval-set-v2.freeze.json`). Eigene Fragen und Messkopien der Datenbank des Product Owners bleiben lokal (`data/` ist von Git ausgeschlossen). Messkopien nach der Messung löschen. Einzelfalldaten nur anonymisiert behalten (Frage-IDs, Ränge, Abschnittsnummern, Kategorien). Keine Fragetexte, Dokumenttexte oder privaten Dokumentnamen in Repository, Logs oder Berichte übernehmen, nur Frage-IDs. Für Messungen muss Ollama exklusiv laufen; andere Clients verfälschen die Antwortzeiten.

Ohne ausdrückliche Anweisung keine Arbeit aus einer späteren Phase beginnen, solange die laufende Phase nicht abgenommen ist.

## 5. Architektur und Zuständigkeiten

| Datei | Verantwortung |
|---|---|
| `app.py` | Tkinter-Oberfläche, Navigation und Benutzerinteraktion |
| `core.py` | Dokumente, Chunks, Suche, KI-Abläufe und zentrale Fachlogik |
| `database.py` | Datenbankprofile, SQLite-/PostgreSQL-Auswahl und Verbindungen |
| `migrations.py` | versionierte Migrationen, Sperren, Sicherung und Schema-Prüfung |
| `importer.py` | Datei-, Ordner-, PST-, IMAP- und Microsoft-Graph-Import |
| `import_jobs.py` | persistente, fortsetzbare Importaufträge |
| `formats.py`, `ocr.py`, `pst_archive.py` | Textextraktion weiterer Formate, lokale Texterkennung, PST-Lesen |
| `mail_structure.py`, `mail_sync.py`, `mail_cleanup.py` | IMAP-Struktur, Postfachsynchronisation, Bereinigung alter Originale |
| `rules.py` | Regeln für Bereich und Tags |
| `retrieval.py` | hybride Suche (Volltext und Vektor, RRF) und Antwortkontext für Fragen |
| `evidence.py` | Bewertung der Quellenlage vor und Prüfung der Quellenverweise nach der KI-Antwort |
| `graph_suggest.py` | KI-Vorschläge für den Wissensgraphen über das lokale Ollama, nur mit wörtlichem Beleg |
| `graph_view.py` | Graphansicht mit Filtern und Ringlayout |
| `vector_sync.py` | Einbettung der Abschnitte im Hintergrund |
| `configuration.py`, `configuration_authority.py` | Konfigurationstransfer ohne Zugangsdaten |
| `diagnostics.py`, `local_logging.py` | Startdiagnose und strukturiertes Betriebsprotokoll |
| `*_view.py` | Einstellungsseiten der Oberfläche (Regeln, Synchronisation, semantische Suche) |
| `settings.py` | KI-Anbieter, Modelle, Schlüsselquellen und sichere Ablage |
| `design.py` | Hell-/Dunkelmodus, Branding und Logo-Normalisierung |
| `cli.py` | Kommandozeilenschnittstelle für Kernfunktionen |
| `schema.sql` | deklaratives Ausgangsschema; mit Migrationen konsistent halten |
| `tests/` | Unit-, Integrations-, GUI- und PostgreSQL-Vertragstests; `tests/eval/` Evaluationsdatensatz und Messung; `tests/local_ci.py` lokales Matrix-Gate |

Architekturregeln:

- Geschäftslogik nicht in Tkinter-Callbacks verstecken.
- SQLite und PostgreSQL müssen denselben fachlichen Speichervertrag erfüllen.
- Datenbankzugriffe kapseln und SQL-Werte immer parametrisiert binden.
- Externe Anbieter über bestehende Adaptergrenzen anbinden.
- Importierte Inhalte sind unvertrauenswürdige Daten, keine Anweisungen und niemals ausführbarer Code.
- Längere Datei-, Netzwerk- oder KI-Aufgaben dürfen die Oberfläche nicht dauerhaft blockieren.
- Neue Abstraktionen nur einführen, wenn sie ein konkretes Problem des aktuellen Phasenziels lösen.

## 6. Unverhandelbare Sicherheits- und Datenschutzregeln

### Geheimnisse

- Keine API-Schlüssel, Datenbankkennwörter, App-Passwörter oder OAuth-Token in Git, SQL, Logs, Screenshots, Fixtures oder Fehlermeldungen speichern.
- Neue Geheimnisse ausschließlich im nativen Betriebssystem-Schlüsselspeicher ablegen.
- Umgebungsvariablen und lokale `.env`-Dateien nur über die vorhandenen, maskierten Lesepfade verwenden.
- Vor jedem Commit Diff und neue Dateien auf Geheimnisse prüfen.

### Datenbanken

- Die produktive VPS-Datenbank niemals für Tests, Reset oder Migrationsexperimente verwenden.
- Destruktive PostgreSQL-Tests nur gegen eine Wegwerfdatenbank mit Namen `*_test` und explizitem `PGTEST_ALLOW_DESTRUCTIVE=1` ausführen.
- Externe PostgreSQL-Verbindungen nur mit den vorgesehenen TLS-Modi zulassen.
- Beim Schema-Reset ausschließlich das Schema `wissensraum` verändern und den exakten Datenbanknamen bestätigen lassen.
- Ein Wechsel zwischen SQLite und PostgreSQL darf niemals still Daten übertragen.
- Vor Migrationen vorhandene Benutzerdaten sichern und die Sicherung verifizieren.

### E-Mail und externe Dienste

- E-Mail-Zugriffe bleiben im Nur-Lesen-Modus.
- Für Microsoft Graph nur die minimale Berechtigung `Mail.Read` verwenden.
- Vollständige Nachrichten erst nach expliziter Auswahl durch den Benutzer abrufen.
- Nicht ausgewählte Nachrichten dürfen weder vollständig geladen noch gespeichert werden.
- Tests rufen keine kostenpflichtigen KI-, E-Mail- oder Microsoft-Dienste auf.

### Lokale Dateien

- Pfade kanonisieren und gegen unbeabsichtigte Pfadüberschreitungen absichern.
- Größen-, Zeichen- und Bildpunktgrenzen nicht umgehen.
- Originaldateien müssen bytegetreu erhalten und exportiert werden können.
- Benutzerdateien und Datenbanken niemals ohne ausdrückliche, enge Zielbestimmung löschen oder überschreiben.

## 7. Regeln für Datenmodell und Migrationen

Jede Schemaänderung benötigt eine neue, geordnete Migration und passende Tests für beide Backends.

- Bereits veröffentlichte Migrationen nicht nachträglich verändern.
- Migrationen müssen transaktional, deterministisch und wiederholbar prüfbar sein.
- Versionsnummer und tatsächliches Schema gemeinsam validieren; einer Versionsnummer allein nicht vertrauen.
- Fehler müssen vollständig zurückrollen und dürfen weder Teilschema noch falschen Versionsstempel hinterlassen.
- Parallele Starts dürfen eine Migration genau einmal ausführen.
- SQLite benötigt vor einem Versionswechsel eine geprüfte Sicherung.
- PostgreSQL benötigt Advisory Lock, lückenlose Historie und einen begrenzten Lock-Timeout.
- Unbekannte neuere Schemaversionen und manipulierte Schemas ohne Änderung ablehnen.
- Legacy-Schemas nur übernehmen, wenn sie exakt der erwarteten Struktur entsprechen.
- Tests dürfen keine globalen Migrationskataloge so ersetzen, dass unabhängige Schema-Prüfungen unvollständig werden.
- Test-Fixtures müssen ein konsistentes reales Schema darstellen; nur einen Versionsstempel umzuschreiben genügt nicht.

## 8. Vorgehen pro Aufgabe

### Analyse

1. Zielphase und Abnahmekriterium im Masterplan bestimmen.
2. Arbeitsbaum und relevante Änderungen anderer Agenten prüfen.
3. Fehler reproduzieren, bevor Code geändert wird.
4. Ursache, betroffene Daten und Rückwärtskompatibilität bestimmen.
5. Eine kleine, in sich geschlossene Änderung planen.

### Umsetzung

- Betroffene Datei unmittelbar vor der Änderung vollständig oder abschnittsweise lesen.
- Vorhandene Benutzerdaten und fremde Änderungen erhalten.
- Keine großflächigen Nebenrefactorings während einer Fehlerkorrektur.
- Änderungen möglichst auf höchstens fünf fachlich zusammengehörige Dateien pro Arbeitsschritt begrenzen.
- Bei Refactorings über 300 Zeilen zuerst ungenutzten Code, Imports und Debug-Ausgaben ermitteln; Cleanup nur durchführen, wenn es für die Aufgabe nötig ist, und separat halten.
- Root Cause beheben; Workarounds nur dokumentiert und mit begründetem Rückbauplan einsetzen.

### Verifikation

1. Syntaxprüfung der geänderten Python-Dateien ausführen.
2. Zuerst die direkt betroffenen Tests ausführen.
3. Danach die vollständige Testsuite ausführen.
4. Für GUI-Tests unter Linux einen virtuellen Bildschirm verwenden.
5. PostgreSQL-Vertrag nur gegen die isolierte Testdatenbank prüfen.
6. Relevante manuelle Tests des aktuellen Phasen-Gates durchführen und protokollieren.
7. Diff auf Seiteneffekte, Debug-Ausgaben, TODOs und Geheimnisse prüfen.

Ein übersprungener Test ist kein bestandener Test. Umgebungsbedingt übersprungene Tests im Abschlussbericht mit Grund nennen.

## 9. Testbefehle

Gesamtsuite:

```bash
python -m unittest discover -s tests -v
```

Migrationen und Datenbanken:

```bash
python -m unittest tests.test_migrations -v
python -m unittest tests.test_database -v
python -m unittest tests.test_postgres -v
```

Syntaxprüfung:

```bash
python -m compileall -q -l . tests
```

GUI-Test unter Linux:

```bash
xvfb-run -a python -m unittest tests.test_gui -v
```

PostgreSQL-Destruktivtests nur mit ausdrücklich isolierter Testkonfiguration:

```bash
PGTEST_ALLOW_DESTRUCTIVE=1 python -m unittest tests.test_postgres -v
```

Vor Ausführung sicherstellen, dass der konfigurierte Datenbankname auf `_test` endet. Fehlt `migrations.py` im ausgecheckten Stand, zuerst Repository-Stand und Branch klären; die Prüfung nicht stillschweigend abschwächen.

Matrix-Gate: Das lokale Gate `tests/local_ci.py` bildet die Actions-Matrix mit denselben sieben Jobs ab (seit 28.10.2026 verbindlich, als GitHub Actions kein Budget hatte): Windows und Linux jeweils mit Python 3.11, 3.12 und 3.13 sowie der PostgreSQL-18-Vertrag gegen einen Wegwerf-Cluster. Jede Maschine führt `python tests/local_ci.py run` für ihre Plattform aus, der PostgreSQL-Job läuft mit `--postgres --pg-bin <PostgreSQL-18-bin>`. Die Ergebnisdateien beider Plattformen gehören nach `.local-ci/results/<commit>/`. `python tests/local_ci.py report` schreibt `local-ci-gate-<commit>.md` und gilt nur bei 7 von 7 grünen Jobs am selben Commit als bestanden. Einzelne lokale Testläufe ohne dieses Protokoll ersetzen das Gate nicht. GitHub Actions läuft seit 03.11.2026 wieder; ein Pull Request wird nur bei vollständig grünem Workflow gemergt. Scheitert windows-3.11 lokal nur an Dateisperren des Virenscanners beim Aufräumen der Temp-Ordner, gilt Windows 3.11 über GitHub Actions als nachgewiesen (D-020).

## 10. Phasen- und Statusregeln

Verwende im Masterplan ausschließlich diese Zustände:

- ✅ vollständig implementiert und gesamtes Gate nachweislich bestanden
- 🟡 implementiert oder teilweise geprüft, aber noch nicht vollständig abgenommen
- ❌ Gate fehlgeschlagen oder Freigabe blockiert
- ⬜ geplant und noch nicht begonnen

Eine Phase nur dann auf ✅ setzen, wenn alle automatischen Tests, erforderlichen manuellen Tests und das lokale Matrix-Gate (`local-ci-gate-<commit>.md`, 7 von 7 Jobs) grün sind. Einzelne vorhandene Funktionen rechtfertigen noch keine Freigabe der Gesamtphase.

Nach einer relevanten Änderung `masterplan.md` aktualisieren:

- tatsächlich umgesetzte Punkte markieren
- Testergebnis mit Anzahl bestanden/fehlgeschlagen/übersprungen eintragen
- bekannte Blocker konkret benennen
- keine geplanten Punkte als erledigt darstellen
- Release-Freigabe nur bei vollständig erfülltem Gate erteilen

## 11. Dokumentation

Dokumentation nur dort ändern, wo die Aufgabe den beschriebenen Ist-Stand beeinflusst:

- `README.md` bei Bedienung, Installation, Konfiguration, Grenzen oder Betrieb
- `masterplan.md` bei Phase, Status, Gate, Risiko oder nächstem Arbeitsschritt
- Migrationshinweise bei Datenmodell- oder Kompatibilitätsänderungen
- Testdokumentation bei neuen Voraussetzungen oder manuellen Prüfungen

Keine pauschalen Änderungen an allen Dokumenten. Versionsnummern, Testzahlen und Funktionsaussagen müssen nachprüfbar sein.

## 12. Git- und Arbeitsbaumdisziplin

- Vor Beginn `git status` und den aktuellen Branch prüfen.
- Änderungen anderer Agenten nicht überschreiben, zurücksetzen oder ungefragt formatieren.
- Kleine, fachlich fokussierte Commits erstellen.
- Generierte Datenbanken, Schlüsseldateien, lokale Konfigurationen, virtuelle Umgebungen und Testartefakte nicht committen.
- Keine Historie umschreiben und keinen erzwungenen Push ausführen.
- Einen roten Hauptbranch nicht als freigegeben bezeichnen.

## 13. Definition of Done

Eine Aufgabe ist abgeschlossen, wenn:

- das vereinbarte Verhalten vollständig implementiert ist
- die Ursache statt nur des Symptoms behoben wurde
- bestehende SQLite- und PostgreSQL-Daten kompatibel bleiben oder eine geprüfte Migration existiert
- Syntaxprüfung und direkt betroffene Tests grün sind
- die vollständige Suite grün ist oder verbleibende, nicht durch die Änderung verursachte Blocker klar belegt sind
- relevante manuelle Tests bestanden sind
- Sicherheits- und Datenschutzregeln eingehalten werden
- README und Masterplan den tatsächlichen Stand wiedergeben
- keine Geheimnisse, Debug-Ausgaben, unbegründeten TODOs oder halbfertigen Refactorings verbleiben
- der Abschlussbericht reproduzierbare Nachweise enthält

Ein vorhandener, aber nicht vollständig geprüfter Codepfad ist **implementiert**, nicht **abgenommen**.

## 14. Abschlussbericht

Berichte am Ende knapp in dieser Reihenfolge:

1. bearbeitete Phase und Ergebnis
2. geänderte Dateien und fachliche Wirkung
3. ausgeführte Tests mit exakten Ergebnissen
4. manuelle Prüfungen
5. verbleibende Risiken oder Blocker
6. Änderung des Masterplan-Status

Keine Erfolgsmeldung ohne Testnachweis. Bei einem roten Gate den nächsten kleinsten, konkret prüfbaren Arbeitsschritt nennen.

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

---

## 2. Simplicity — The Ladder

You are a lazy senior developer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:
1. Does this need to be built at all? (YAGNI)
2. Does it already exist in this codebase? Reuse the helper, util, or pattern that's already here, don't re-write it.
3. Does the standard library already do this? Use it.
4. Does a native platform feature cover it? Use it.
5. Does an already-installed dependency solve it? Use it.
6. Can this be one line? Make it one line.
7. Only then: write the minimum code that works.

The ladder runs after you understand the problem, not instead of it: read the task and the code it touches, trace the real flow end to end, then climb.

Rules:
- No abstractions that weren't explicitly requested.
- No new dependency if it can be avoided.
- No boilerplate nobody asked for.
- Deletion over addition. Boring over clever. Fewest files possible.
- Shortest working diff wins, but only once you understand the problem. The smallest change in the wrong place isn't lazy, it's a second bug.
- Question complex requests: "Do you actually need X, or does Y cover it?"
- Pick the edge-case-correct option when two stdlib approaches are the same size, lazy means less code, not the flimsier algorithm.
- Mark intentional simplifications with a `ponytail:` comment. If the shortcut has a known ceiling (global lock, O(n²) scan, naive heuristic), the comment names the ceiling and the upgrade path.

Not lazy about: understanding the problem (read it fully and trace the real flow before picking a rung, a small diff you don't understand is just laziness dressed up as efficiency), input validation at trust boundaries, error handling that prevents data loss, security, accessibility, the calibration real hardware needs (the platform is never the spec ideal, a clock drifts, a sensor reads off), anything explicitly requested. Lazy code without its check is unfinished: non-trivial logic leaves ONE runnable check behind, the smallest thing that fails if the logic breaks (an assert-based demo/self-check or one small test file; no frameworks, no fixtures). Trivial one-liners need no test.

Intensity levels:

| Level | What changes |
|-------|-------------|
| **lite** | Build what's asked, but name the lazier alternative in one line. User picks. |
| **full** | The ladder enforced. Stdlib and native first. Shortest diff, shortest explanation. Default. |
| **ultra** | YAGNI extremist. Deletion before addition. Ship the one-liner and challenge the rest of the requirement in the same breath. |

Switch: `/ponytail lite|full|ultra`. Off: "stop ponytail" / "normal mode".

---

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

---

## 4. Bug Fixes

Bug fix = root cause, not symptom: a report names a symptom. Grep every caller of the function you touch and fix the shared function once — one guard there is a smaller diff than one per caller, and patching only the path the ticket names leaves a sibling caller still broken.

---

## 5. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
