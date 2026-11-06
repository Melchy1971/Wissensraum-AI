# Lokales Matrix-Gate Wissensraum-AI

- Commit: `2aadc5c`
- Erstellt: 06.11.2026 09:33:36
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 616 | 36 | 9 | 3.11.15 Windows-10-10.0.26300-SP0 | 354 s |
| windows-3.12 | PASS | 616 | 36 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 233 s |
| windows-3.13 | PASS | 616 | 36 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 229 s |
| linux-3.11 | PASS | 616 | 32 | 0 | 3.11.15 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 87 s |
| linux-3.12 | PASS | 616 | 32 | 0 | 3.12.13 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 84 s |
| linux-3.13 | PASS | 616 | 32 | 0 | 3.13.14 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 83 s |
| postgres | PASS | 32 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 9 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_round_trip_exports_every_allowlisted_domain_without_reading_or_changing_secrets`
- windows-3.11: `test_core.KnowledgeTests.test_delete_removes_fts_and_preserves_other_document`
- windows-3.11: `test_diagnostics.DiagnosticTests.test_legacy_sqlite_versions_warn_without_mutation_then_migrate`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_repeated_build_has_stable_semantic_hash_despite_timestamp_metadata`
- windows-3.11: `test_migrations.SQLiteRestoreTests.test_failed_verification_returns_to_previous_state`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_full_text_runs_while_the_question_is_embedded`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_hybrid_contract`
- windows-3.11: `test_vector_sync.VectorSyncTests.test_activation_probes_ollama_and_stays_off_when_it_fails`
- windows-3.11: `test_versions.ImportVersionTests.test_crash_after_storing_a_version_resumes_without_a_second_document`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmp069l2f8o\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmp1o3_z8s4\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpm21llf74\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 29 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 29 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 29 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
