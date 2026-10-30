# Lokales Matrix-Gate Wissensraum-AI

- Commit: `17aa5d2`
- Erstellt: 30.10.2026 15:29:32
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 550 | 29 | 0 | 3.11.9 Windows-10-10.0.26300-SP0 | 198 s |
| windows-3.12 | PASS | 550 | 29 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 191 s |
| windows-3.13 | PASS | 550 | 29 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 191 s |
| linux-3.11 | PASS | 550 | 25 | 0 | 3.11.15 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 180 s |
| linux-3.12 | PASS | 550 | 25 | 0 | 3.12.13 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 149 s |
| linux-3.13 | PASS | 550 | 25 | 0 | 3.13.14 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 | 161 s |
| postgres | PASS | 26 | 0 | 0 | 3.13.18 Linux-6.18.44-fc-v185-x86_64-with-glibc2.39, postgres (PostgreSQL) 18.6 | 19 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmp_i_yiqm9\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpaqy7qu87\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 23 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmp0soxpmct\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 23 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 2 × Windows path equivalence only
