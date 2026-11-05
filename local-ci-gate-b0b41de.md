# Lokales Matrix-Gate Wissensraum-AI

- Commit: `b0b41de`
- Erstellt: 05.11.2026 12:14:33
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 598 | 31 | 5 | 3.11.15 Windows-10-10.0.26300-SP0 | 245 s |
| windows-3.12 | PASS | 598 | 31 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 239 s |
| windows-3.13 | PASS | 598 | 31 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 213 s |
| linux-3.11 | PASS | 598 | 27 | 0 | 3.11.15 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 135 s |
| linux-3.12 | PASS | 598 | 27 | 0 | 3.12.13 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 129 s |
| linux-3.13 | PASS | 598 | 27 | 0 | 3.13.14 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 131 s |
| postgres | PASS | 27 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 17 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_core.KnowledgeTests.test_folder_import_is_recursive_and_keeps_relative_paths`
- windows-3.11: `test_core.KnowledgeTests.test_store_original_and_deduplicate`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_valid_content_alias_satisfies_source_without_creating_content`
- windows-3.11: `test_mail_jobs.MailJobTests.test_message_with_attachments_only_is_imported_with_names`
- windows-3.11: `test_mail_sync.MailSyncTests.test_only_new_messages_without_attachments_and_without_duplicates`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 24 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmp3owqn022\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 24 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpfx0m3yz2\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 24 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpe_bibd7a\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 24 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 24 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 24 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
