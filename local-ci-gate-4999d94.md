# Lokales Matrix-Gate Wissensraum-AI

- Commit: `4999d94`
- Erstellt: 06.11.2026 14:11:11
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 628 | 39 | 9 | 3.11.15 Windows-10-10.0.26300-SP0 | 274 s |
| windows-3.12 | PASS | 628 | 39 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 237 s |
| windows-3.13 | PASS | 628 | 39 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 236 s |
| linux-3.11 | PASS | 628 | 35 | 0 | 3.11.15 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 94 s |
| linux-3.12 | PASS | 628 | 35 | 0 | 3.12.13 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 100 s |
| linux-3.13 | PASS | 628 | 35 | 0 | 3.13.14 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 93 s |
| postgres | PASS | 35 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 11 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_account_payload_order_does_not_break_transaction_verification`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_restore_verification_and_journal_cleanup_failures_are_fatal_and_recoverable`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_same_backend_profile_change_requires_restart_but_noop_does_not`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_repeated_build_has_stable_semantic_hash_despite_timestamp_metadata`
- windows-3.11: `test_importer.ImportTests.test_every_import_channel_records_exactly_one_expected_source`
- windows-3.11: `test_mail_cleanup.MailCleanupTests.test_cli_dry_run`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_ask_uses_the_same_retrieval`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_new_vectors_and_another_database_invalidate_the_cache`
- windows-3.11: `test_settings.SettingsTests.test_save_unchanged_external_key_does_not_require_vault_write`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 32 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpe9n_m72x\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 32 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmphr4n7mg8\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 32 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpr3ktv0yw\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 32 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 32 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 32 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
