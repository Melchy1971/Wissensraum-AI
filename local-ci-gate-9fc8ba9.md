# Lokales Matrix-Gate Wissensraum-AI

- Commit: `9fc8ba9`
- Erstellt: 05.11.2026 15:35:56
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 614 | 35 | 9 | 3.11.15 Windows-10-10.0.26300-SP0 | 284 s |
| windows-3.12 | PASS | 614 | 35 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 234 s |
| windows-3.13 | PASS | 614 | 35 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 236 s |
| linux-3.11 | PASS | 614 | 31 | 0 | 3.11.15 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 127 s |
| linux-3.12 | PASS | 614 | 31 | 0 | 3.12.13 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 129 s |
| linux-3.13 | PASS | 614 | 31 | 0 | 3.13.14 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 133 s |
| postgres | PASS | 31 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 18 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_eval_dataset.EvaluationDatasetTests.test_baseline_runs_through_the_product_search`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_invalid_alias_hash_and_unknown_target_stay_missing`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_unsupported_and_duplicate_manifest_entries_do_not_block_ready`
- windows-3.11: `test_evidence.EvidenceTests.test_context_limits_sections_neighbours_and_documents`
- windows-3.11: `test_logging.LoggingTests.test_reopening_with_smaller_retention_prunes_old_numbered_backups`
- windows-3.11: `test_mail_attachments.AttachmentReleaseTests.test_deleting_the_email_keeps_the_attachment_document`
- windows-3.11: `test_mail_sync.MailSyncTests.test_unreachable_server_is_retried_at_the_next_interval`
- windows-3.11: `test_migrations.SQLiteRestoreTests.test_damaged_target_is_not_overwritten`
- windows-3.11: `test_relations.RelationTests.test_contract`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 28 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpwidj0ho_\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 28 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpjesqycl1\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 28 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmppmawn9mi\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 28 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 28 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 28 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
