# Lokales Matrix-Gate Wissensraum-AI

- Commit: `c7ef78a`
- Erstellt: 29.10.2026 16:16:45
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 543 | 29 | 0 | 3.11.9 Windows-10-10.0.26300-SP0 | 190 s |
| windows-3.12 | PASS | 543 | 29 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 188 s |
| windows-3.13 | PASS | 543 | 29 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 187 s |
| linux-3.11 | PASS | 543 | 25 | 0 | 3.11.15 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 126 s |
| linux-3.12 | PASS | 543 | 25 | 0 | 3.12.13 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 131 s |
| linux-3.13 | PASS | 543 | 25 | 0 | 3.13.14 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 126 s |
| postgres | PASS | 26 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 14 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpyoidm6a0\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmp28z4cwog\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmpq8yc4dg8\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 2 × Windows path equivalence only
