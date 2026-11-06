# Lokales Matrix-Gate Wissensraum-AI

- Commit: `282f8b8`
- Erstellt: 06.11.2026 13:18:11
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 625 | 38 | 9 | 3.11.15 Windows-10-10.0.26300-SP0 | 268 s |
| windows-3.12 | PASS | 625 | 38 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 241 s |
| windows-3.13 | PASS | 625 | 38 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 233 s |
| linux-3.11 | PASS | 625 | 34 | 0 | 3.11.15 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 92 s |
| linux-3.12 | PASS | 625 | 34 | 0 | 3.12.13 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 90 s |
| linux-3.13 | PASS | 625 | 34 | 0 | 3.13.14 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39 | 90 s |
| postgres | PASS | 34 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v242-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 11 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Wiederholte Tests (nur WinError 145 beim Aufräumen)

- windows-3.11: `test_configuration.ConfigurationTransferTests.test_authority_versions_require_exact_int_type`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_deep_and_excessive_json_structures_are_redacted`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_round_trip_exports_every_allowlisted_domain_without_reading_or_changing_secrets`
- windows-3.11: `test_configuration.ConfigurationTransferTests.test_transfer_validation_rejects_hostile_subclasses_without_hooks`
- windows-3.11: `test_core.KnowledgeTests.test_store_original_and_deduplicate`
- windows-3.11: `test_diagnostics.HealthOverviewTests.test_configured_state_passes_and_never_exposes_keys`
- windows-3.11: `test_eval_stock_builder.EvaluationStockBuilderTests.test_old_schema_is_migrated_only_in_output`
- windows-3.11: `test_ocr.OcrRecognitionTests.test_released_image_attachment_is_read_with_ocr`
- windows-3.11: `test_relations.RelationTests.test_contract`

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 31 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpjtzjskwd\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 31 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpb4vm8tag\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 31 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\wt-h\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpqvd6nhrz\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 31 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 31 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 31 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
