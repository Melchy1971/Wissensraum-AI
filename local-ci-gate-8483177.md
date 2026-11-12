# Lokales Matrix-Gate Wissensraum-AI

- Commit: `8483177`
- Erstellt: 12.11.2026 14:41:03
- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`

| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |
|---|---|---:|---:|---:|---|---:|
| windows-3.11 | PASS | 812 | 62 | 0 | 3.11.9 Windows-10-10.0.26300-SP0 | 592 s |
| windows-3.12 | PASS | 812 | 62 | 0 | 3.12.10 Windows-11-10.0.26300-SP0 | 534 s |
| windows-3.13 | PASS | 812 | 62 | 0 | 3.13.8 Windows-11-10.0.26300-SP0 | 575 s |
| linux-3.11 | PASS | 812 | 58 | 0 | 3.11.15 Linux-6.18.44-fc-v378-x86_64-with-glibc2.39 | 186 s |
| linux-3.12 | PASS | 812 | 58 | 0 | 3.12.3 Linux-6.18.44-fc-v378-x86_64-with-glibc2.39 | 193 s |
| linux-3.13 | PASS | 812 | 58 | 0 | 3.13.14 Linux-6.18.44-fc-v378-x86_64-with-glibc2.39 | 196 s |
| postgres | PASS | 58 | 0 | 0 | 3.13.8 Windows-11-10.0.26300-SP0, postgres (PostgreSQL) 18.7 | 180 s |

**Gesamtergebnis: PASS, 7 von 7 Jobs grün**

## Übersprungene Tests

SKIP ist kein bestandener Test.

- windows-3.11: 1 × POSIX symlink behavior only
- windows-3.11: 55 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.11: 1 × file has no distinct 8.3 alias
- windows-3.11: 1 × open-file replacement unsupported on Windows
- windows-3.11: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.11\\\\tmpbqxpwuow\\\\application.jsonl'
- windows-3.11: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.12: 1 × POSIX symlink behavior only
- windows-3.12: 55 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.12: 1 × file has no distinct 8.3 alias
- windows-3.12: 1 × open-file replacement unsupported on Windows
- windows-3.12: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.12\\\\tmpsnm9q0rj\\\\application.jsonl'
- windows-3.12: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- windows-3.13: 1 × POSIX symlink behavior only
- windows-3.13: 55 × PostgreSQL-Testdienst nicht konfiguriert
- windows-3.13: 1 × file has no distinct 8.3 alias
- windows-3.13: 1 × open-file replacement unsupported on Windows
- windows-3.13: 1 × open-file replacement unsupported: [WinError 32] Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird: 'H:\\\\Wissensraum-AI\\\\.local-ci\\\\tmp\\\\windows-3.13\\\\tmp08m_nm7h\\\\application.jsonl'
- windows-3.13: 3 × sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen
- linux-3.11: 55 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.11: 1 × Windows 8.3 aliases only
- linux-3.11: 2 × Windows path equivalence only
- linux-3.12: 55 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.12: 1 × Windows 8.3 aliases only
- linux-3.12: 2 × Windows path equivalence only
- linux-3.13: 55 × PostgreSQL-Testdienst nicht konfiguriert
- linux-3.13: 1 × Windows 8.3 aliases only
- linux-3.13: 2 × Windows path equivalence only
