# Lokales Matrix-Gate Wissensraum-AI

- Commit: `84184d2`
- Erstellt: 06.11.2026 08:20:07
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 616 | 36 | 9 | 3.11.15 Windows-10-10.0.26300-SP0 | 245 s |
| windows-3.12 | PASS | 616 | 36 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 219 s |
| windows-3.13 | PASS | 616 | 36 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 226 s |
| linux-3.11 | PASS | 616 | 32 | 0 | 3.11.15 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 86 s |
| linux-3.12 | PASS | 616 | 32 | 0 | 3.12.13 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 84 s |
| linux-3.13 | PASS | 616 | 32 | 0 | 3.13.14 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 88 s |
| postgres | PASS | 32 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 9 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_account_payload_order_does_not_break_transaction_verification`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_deep_and_excessive_json_structures_are_redacted`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_round_trip_exports_every_allowlisted_domain_without_reading_or_changing_secrets`
- windows-3.11: `test_diagnostics.DiagnosticTests.test_sqlite_health_rejects_newer_and_incompatible_legacy_schema`
- windows-3.11: `test_logging.LoggingTests.test_secret_scanning_across_every_rotated_artifact`
- windows-3.11: `test_mail_jobs.MailJobTests.test_imap_job_imports_selection_and_only_released_attachments`
- windows-3.11: `test_mail_jobs.MailJobTests.test_message_without_text_or_attachments_is_quarantined`
- windows-3.11: `test_vector_sync.VectorSyncTests.test_failure_keeps_activation_and_retries_later`
- windows-3.11: `test_versions.StoreVersionTests.test_list_and_search_return_only_the_current_version_by_default`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpkyoolu9m\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpswfxggq5\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 29 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpmtnhddf7\\\\application.jsonl'
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
