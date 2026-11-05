# Lokales Matrix-Gate Wissensraum-AI

- Commit: `7f6c43c`
- Erstellt: 05.11.2026 13:06:03
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 602 | 32 | 7 | 3.11.15 Windows-10-10.0.26300-SP0 | 350 s |
| windows-3.12 | PASS | 602 | 32 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 233 s |
| windows-3.13 | PASS | 602 | 32 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 208 s |
| linux-3.11 | PASS | 602 | 28 | 0 | 3.11.15 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 138 s |
| linux-3.12 | PASS | 602 | 28 | 0 | 3.12.13 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 130 s |
| linux-3.13 | PASS | 602 | 28 | 0 | 3.13.14 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 | 134 s |
| postgres | PASS | 28 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v236-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 16 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_same_backend_profile_change_requires_restart_but_noop_does_not`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_transfer_validation_rejects_hostile_subclasses_without_hooks`
- windows-3.11: `test_evidence.EvidenceTests.test_refusal_and_invalid_answers_are_not_retried`
- windows-3.11: `test_evidence.EvidenceTests.test_technical_failure_is_retried_exactly_once`
- windows-3.11: `test_logging.LoggingTests.test_reopening_with_smaller_retention_prunes_old_numbered_backups`
- windows-3.11: `test_retrieval.SqliteRetrievalTests.test_new_vectors_and_another_database_invalidate_the_cache`
- windows-3.11: `test_vector_sync.VectorSyncTests.test_failure_keeps_activation_and_retries_later`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 25 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmprt01m5kk\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 25 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpf29m2g29\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 25 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpkx59rynv\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 25 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 25 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 25 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
