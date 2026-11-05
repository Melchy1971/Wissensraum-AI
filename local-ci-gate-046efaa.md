# Lokales Matrix-Gate Wissensraum-AI

- Commit: `046efaa`
- Erstellt: 05.11.2026 14:01:21
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 605 | 33 | 17 | 3.11.15 Windows-10-10.0.26300-SP0 | 420 s |
| windows-3.12 | PASS | 605 | 33 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 353 s |
| windows-3.13 | PASS | 605 | 33 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 344 s |
| linux-3.11 | PASS | 605 | 29 | 0 | 3.11.15 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 149 s |
| linux-3.12 | PASS | 605 | 29 | 0 | 3.12.13 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 140 s |
| linux-3.13 | PASS | 605 | 29 | 0 | 3.13.14 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 137 s |
| postgres | PASS | 29 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 20 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_cli.CliRestoreTests.test_status_reports_health_and_fails_only_on_failures`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_account_payload_order_does_not_break_transaction_verification`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_active_authority_owner_blocks_independent_process_mutation`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_pending_transfer_journal_contains_only_semantic_snapshots`
- windows-3.11: `test_core.KnowledgeTests.test_delete_removes_fts_and_preserves_other_document`
- windows-3.11: `test_core.KnowledgeTests.test_search_fts_and_special_characters`
- windows-3.11: `test_eval_dataset.OwnQuestionTests.test_own_questions_use_product_search_and_report_missing_sources`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_conflicting_and_chained_aliases_fail_closed`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_repeated_build_has_stable_semantic_hash_despite_timestamp_metadata`
- windows-3.11: `test_evidence.EvidenceTests.test_first_and_last_section_have_one_candidate`
- windows-3.11: `test_importer.ImportTests.test_account_save_busy_lock_precedes_vault_sql_and_authority_mutation`
- windows-3.11: `test_importer.ImportTests.test_every_import_channel_records_exactly_one_expected_source`
- windows-3.11: `test_mail_sync.MailSyncTests.test_microsoft_inbox_only_messages_after_activation`
- windows-3.11: `test_mail_sync.MailSyncTests.test_sign_in_failure_stops_with_reason_until_reactivated`
- windows-3.11: `test_rules.RuleStoreTests.test_revert_keeps_a_newer_area_and_skips_rows_changed_since_preview`
- windows-3.11: `test_versions.StoreVersionTests.test_deleting_the_current_version_makes_the_previous_one_current`
- windows-3.11: `test_versions.StoreVersionTests.test_without_versioning_or_place_documents_stay_independent`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 26 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpyw7admp8\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 26 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpif9stmxr\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 26 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpfne7v8b4\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 26 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 26 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 26 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
