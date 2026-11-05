# Lokales Matrix-Gate Wissensraum-AI

- Commit: `10253b7`
- Erstellt: 05.11.2026 15:00:29
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 609 | 34 | 13 | 3.11.15 Windows-10-10.0.26300-SP0 | 288 s |
| windows-3.12 | PASS | 609 | 34 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 242 s |
| windows-3.13 | PASS | 609 | 34 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 243 s |
| linux-3.11 | PASS | 609 | 30 | 0 | 3.11.15 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 157 s |
| linux-3.12 | PASS | 609 | 30 | 0 | 3.12.13 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 144 s |
| linux-3.13 | PASS | 609 | 30 | 0 | 3.13.14 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 147 s |
| postgres | PASS | 30 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 19 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_chunking.ChunkingTests.test_contract`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_deep_and_excessive_json_structures_are_redacted`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_export_file_is_atomic_and_does_not_leave_temporary_file`
- windows-3.11: `test_core.KnowledgeTests.test_question_sends_only_retrieved_sources_and_logs_snapshot`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_manifest_rejects_database_as_input_and_paths_outside_data_for_control_files`
- windows-3.11: `test_logging.LoggingTests.test_secret_scanning_across_every_rotated_artifact`
- windows-3.11: `test_mail_attachments.AttachmentReleaseTests.test_graph_import_downloads_only_released_attachments`
- windows-3.11: `test_mail_cleanup.MailCleanupTests.test_cli_dry_run`
- windows-3.11: `test_mail_jobs.MailJobTests.test_crash_after_storing_a_message_resumes_as_imported`
- windows-3.11: `test_migrations.SQLiteRestoreTests.test_older_backup_is_restored_as_is_and_migrated_on_next_start`
- windows-3.11: `test_vector_sync.VectorSyncTests.test_model_change_recomputes_and_deactivation_keeps_vectors`
- windows-3.11: `test_versions.StoreVersionTests.test_deleting_the_current_version_makes_the_previous_one_current`
- windows-3.11: `test_versions.StoreVersionTests.test_match_predicts_add_without_writing`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 27 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpvj3pycyn\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 27 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmp27e2llsr\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 27 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpfprui1f6\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 27 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 27 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 27 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
