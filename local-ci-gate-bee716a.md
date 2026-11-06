# Lokales Matrix-Gate Wissensraum-AI

- Commit: `bee716a`
- Erstellt: 06.11.2026 14:47:03
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 639 | 41 | 14 | 3.11.15 Windows-10-10.0.26300-SP0 | 290 s |
| windows-3.12 | PASS | 639 | 41 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 252 s |
| windows-3.13 | PASS | 639 | 41 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 250 s |
| linux-3.11 | PASS | 639 | 37 | 0 | 3.11.15 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 97 s |
| linux-3.12 | PASS | 639 | 37 | 0 | 3.12.13 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 95 s |
| linux-3.13 | PASS | 639 | 37 | 0 | 3.13.14 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 98 s |
| postgres | PASS | 37 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 12 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_active_authority_owner_blocks_independent_process_mutation`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_export_lock_blocks_an_independent_process_and_releases_after_exit`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_export_never_serializes_environment_derived_database_profile`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_import_lock_blocks_an_independent_process_and_releases_after_exit`
- windows-3.11: `test_core.KnowledgeTests.test_backup_includes_originals_and_index`
- windows-3.11: `test_core.KnowledgeTests.test_question_sends_only_retrieved_sources_and_logs_snapshot`
- windows-3.11: `test_core.KnowledgeTests.test_summary_and_length_guard`
- windows-3.11: `test_diagnostics.HealthOverviewTests.test_ollama_needs_no_key_and_broken_store_fails_closed`
- windows-3.11: `test_eval_dataset.EvaluationDatasetTests.test_baseline_runs_through_the_product_search`
- windows-3.11: `test_evidence.EvidenceTests.test_neighbour_before_or_after_the_hit_by_word_coverage`
- windows-3.11: `test_import_jobs.ImportJobTests.test_folder_job_classifies_every_file_and_records_source_and_checksum`
- windows-3.11: `test_mail_cleanup.MailCleanupTests.test_message_that_would_lose_text_is_skipped`
- windows-3.11: `test_migrations.SQLiteRestoreTests.test_older_backup_is_restored_as_is_and_migrated_on_next_start`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_wrong_dimension_falls_back_and_drops_the_cache`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 34 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpc6njesm_\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 34 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpvhegn_b_\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 34 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpd0my_ob4\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 34 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 34 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 34 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
