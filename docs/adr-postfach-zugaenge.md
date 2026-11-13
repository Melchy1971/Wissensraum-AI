# ADR: Postfach-Zugänge im Server (Paritäts-Roadmap Phase 8)

Status: angenommen am 11.11.2026 (D-030): Option B mit A für IMAP, PST nur im Desktop, Zustimmung zu `Mail.Read` und `offline_access` erteilt der Product Owner
Stand: 11.11.2026
Betrifft: D06 bis D10, D12 der Paritätsmatrix, Befund „Geheimnisablage je Konto“ im Architektur-Review

## 1. Ausgangslage im Code

Der Desktop speichert Postfach-Zugänge im Schlüsselspeicher des Betriebssystems (`settings.SystemVault`, `keyring`). Er lässt nur native Speicher zu und lehnt Dateispeicher ab. Gespeichert werden:

| Zugang | Inhalt | Quelle im Code |
|---|---|---|
| IMAP | App-Kennwort, bei Gmail Pflichtformat | `importer.MailAccounts._save_owned` |
| Microsoft 365 | MSAL-Token-Cache mit Refresh-Token für `Mail.Read`, Anmeldung per Gerätecode an einer öffentlichen Client-App mit `client_id` je Postfach | `importer.MailAccounts.begin_microsoft`, `microsoft_token` |
| PST | kein Geheimnis, aber eine Datei, oft mehrere GB, gelesen mit `libpff-python` | `pst_archive.py`, `requirements-pst.txt` |

Im Server-Container gibt es keinen Schlüsselspeicher. Die Tabelle `email_accounts` hat keine Spalte für das Konto, dem ein Postfach gehört. Die Anmeldung am Server fragt bei Entra nur `openid profile` an (`oidc.py`). Uploads sind auf 100 MB begrenzt (Traefik und `web.py`), eine PST passt also nicht durch den Upload.

## 2. Anforderungen

1. Ein Zugang gehört genau einem Konto. Kein anderes Konto, auch kein Admin, kann ihn benutzen oder lesen. Ein Admin kann ihn nur löschen.
2. Geheimnisse stehen nie im Klartext in der Datenbank, in Sicherungen, im Audit, im Log oder im Installationsexport.
3. Der Worker muss für die Synchronisation im Intervall (D09) ohne anwesenden Benutzer an das Geheimnis kommen.
4. Ein gestohlener `pg_dump` allein verrät keinen Zugang.
5. Entzug wirkt sofort: Konto gesperrt oder Zugang gelöscht, dann läuft kein Abruf mehr.
6. Wiederherstellung aus der Sicherung muss möglich bleiben, ohne dass die Sicherung die Geheimnisse lesbar enthält.

## 2a. Konflikt mit den Projektregeln

`CLAUDE.md` Abschnitt 6 verlangt: „Neue Geheimnisse ausschließlich im nativen Betriebssystem-Schlüsselspeicher ablegen.“ Den gibt es im Container nicht. Jede Option, die Zugänge im Server speichert (A, B, D), braucht deshalb eine ausdrückliche Ausnahme dieser Regel für den Server, als neue Entscheidung D-0xx im Masterplan und als Ergänzung in `CLAUDE.md` und `docs/sicherheit-3.0.md`. Nur Option C kommt ohne gespeicherte Geheimnisse aus. Der Desktop bleibt in jedem Fall beim Schlüsselspeicher.

Weitere Regeln aus Abschnitt 6, die jede Option einhalten muss: nur Lesen, für Microsoft Graph nur `Mail.Read` (`offline_access` ist kein Graph-Recht, sondern erlaubt das Refresh-Token), vollständige Nachrichten erst nach ausdrücklicher Auswahl. Die Synchronisation im Intervall (D09) gibt es im Desktop schon mit ausdrücklichem Einschalten je Postfach. Im Server gilt dasselbe: ohne Einschalten durch das Konto kein Abruf.

## 3. Optionen

### A. Verschlüsselt in PostgreSQL, Schlüssel als Docker-Secret

Neue Tabelle mit Postfach, Konto und Chiffrat. Verschlüsselung mit AES-256-GCM aus `cryptography` (ist über `PyJWT[crypto]` schon im Image, Version 50.0.2). Der Schlüssel liegt als Docker-Secret `mail_key` nur bei Server und Worker, nicht in der Datenbank. Zusätzliche Daten der Verschlüsselung (AAD) sind Postfach-ID und Konto-ID, damit ein Chiffrat nicht in eine andere Zeile kopiert werden kann. Jedes Chiffrat trägt eine Schlüsselkennung, damit der Schlüssel gewechselt werden kann.

- Erfüllt 1 bis 6. Für 6 braucht es eine Kopie des Schlüssels außerhalb des VPS, verschlüsselt mit age wie die Sicherung.
- Grenze: Wer den laufenden Server oder Worker übernimmt, hat Schlüssel und Datenbank und damit alle Zugänge. Das gilt für jede Lösung, die D09 ohne anwesenden Benutzer erfüllt.
- Aufwand: 2 bis 3 PT für Ablage, Schlüsselwechsel und Tests.

### B. Wie A, Microsoft aber über die bestehende Entra-App

Wie A. Für Microsoft 365 entfällt die Gerätecode-Anmeldung mit eigener `client_id` je Postfach. Das Konto verbindet sein eigenes Postfach über die Server-App (vertraulicher Client mit vorhandenem Client-Geheimnis), mit nachträglicher Zustimmung zu `Mail.Read` und `offline_access`. Gespeichert wird das Refresh-Token, verschlüsselt wie in A.

- Zusätzlich: Ein Konto kann nur das Postfach lesen, mit dem es sich bei Entra anmeldet. Fremde Postfächer sind ausgeschlossen, ohne dass der Server das prüfen muss.
- Erfordert eine Änderung der App-Registrierung in Entra: delegierte Berechtigung `Mail.Read`, je nach Mandant Zustimmung durch den Mandanten-Admin.
- Aufwand: 2 bis 3 PT mehr als A.

### C. Keine gespeicherten Geheimnisse

Ein Abruf läuft nur, solange der Benutzer ihn anstößt. Das IMAP-Kennwort oder der Microsoft-Token gehen mit dem Auftrag an den Worker, liegen bis zum Ende des Auftrags verschlüsselt in der Datenbank und werden danach gelöscht.

- Erfüllt 1, 2, 4, 5 am stärksten. D09 (Intervall) entfällt, D10 schrumpft auf Postfachangaben ohne Kennwort.
- Ein langer Abruf, der durch einen Neustart unterbrochen wird, braucht eine neue Eingabe.

### D. Externer Geheimnisdienst (Vault, Infisical)

Eigener Dienst auf dem VPS mit eigener Sicherung und eigenem Betrieb. Schützt nicht besser gegen die Übernahme des Servers als A, bringt aber einen weiteren Dienst. Für eine Installation mit wenigen Konten nicht verhältnismäßig.

## 4. PST

PST-Dateien enthalten kein Geheimnis, sind aber zu groß für den Upload. Drei Wege:

1. Admin legt die Datei per SFTP in einen Importordner auf dem VPS, der Worker liest sie von dort. Einfach, aber nur für Admins.
2. Upload in Teilen (zum Beispiel 50 MB) mit Zusammensetzen im Worker. 3 bis 5 PT mehr, für jedes Konto nutzbar.
3. PST nur im Desktop, Ergebnis über die Sammlungsfreigabe teilen. Kein Aufwand im Server.

`libpff-python` müsste ins Image. Die Lizenz ist LGPL-3.0-or-later, als unveränderte Bibliothek unkritisch, aber zu vermerken.

## 5. Empfehlung

B für Microsoft 365 und A für IMAP, PST zunächst über Weg 3 (nur Desktop). Begründung: B bindet jedes Konto an sein eigenes Postfach und spart die Gerätecode-Anmeldung, A ist für IMAP die kleinste Lösung, die D09 erfüllt. PST über den Server lohnt erst, wenn es Bedarf gibt.

Folgen bei Annahme:

- Ausnahme von der Schlüsselspeicher-Regel für den Server (Abschnitt 2a), als Entscheidung im Masterplan und in `CLAUDE.md`.
- Migration 26: Spalte `account_id` an `email_accounts`, neue Tabelle für Chiffrate, Abrufstand je Konto.
- Neues Docker-Secret `mail_key`, Kopie mit age außerhalb des VPS, Eintrag in `deploy/BETRIEB.md`.
- Entra: delegierte Berechtigung `Mail.Read` und `offline_access` an der Server-App.
- Der Installationsexport (Phase 5) und `privacy.account_report` lassen die Chiffrate aus.
- Audit: `mailbox_changed` und `mail_import_started`, nie mit Adresse oder Kennwort.

Aufwand Phase 8 mit Empfehlung: 9 bis 14 PT statt 15 bis 25 PT laut Roadmap.

Umsetzung (11.11.2026): Migration 26 legt nur die Tabelle `account_mailboxes` an (Besitz und Chiffrat), `email_accounts` bleibt unverändert. Audit-Ereignis heißt `mailbox_changed`, Importe laufen als `import_started` mit `mail`. D12 (Anhänge alter E-Mails entfernen) entfällt im Server, weil er Anhänge nie im Original speichert.

## 5a. Nachtrag 13.11.2026: private Microsoft-Konten (D-032)

Anlass: Der Product Owner meldet sich als Gast mit einem Hotmail-Konto am Mandanten an, der Mandant hat kein Exchange. Das Postfach liegt bei Outlook.com. Ein Token aus dem Mandanten erreicht es nicht, und Outlook.com nimmt seit 16.09.2024 kein IMAP mit Kennwort mehr an. Ohne Erweiterung gibt es für ihn keinen Postfachzugang.

Entscheidung: Dieselbe Entra-App verbindet Outlook.com-Postfächer über die Autorität `https://login.microsoftonline.com/consumers/v2.0`, nur mit `WR_MAIL_PERSONAL_ACCOUNTS=1`. Die App muss dafür auch private Microsoft-Konten zulassen. Die Anmeldung am Server ändert sich nicht: eigene Autorität des Mandanten und Prüfung von `tid`.

- Eigener Start und Rückruf: `/postfaecher/microsoft/privat/verbinden` und `/postfaecher/microsoft/privat/rueckruf`. Zustand, Nonce und PKCE wie beim Mandanten, der Zustand ist an den Browser gebunden.
- Das ID-Token muss von der consumers-Autorität stammen und den festen Mandanten privater Konten (`9188040d-6c67-4c5b-b112-36a304b66dad`) tragen.
- Die Gleichheit von `tid` und `oid` mit dem angemeldeten Konto entfällt für private Konten. Ein privates Konto hat außerhalb des Mandanten eine eigene Identität, die zum Gastkonto nicht passt. Es gilt dieselbe Regel wie bei IMAP: Das Postfach gehört dem angemeldeten Server-Konto, und zustimmen kann nur, wer die Microsoft-Zugangsdaten des Postfachs hat.
- `email_accounts.tenant` steht für solche Postfächer auf `consumers`. Server und Worker erneuern das Token dort, nie beim Mandanten. Ohne eingeschalteten Weg lehnt der Worker ein solches Postfach ab, statt den Mandanten zu fragen.
- Audit: `mailbox_changed` mit `provider` `microsoft_personal`.
- Keine Migration, die Spalte `tenant` gab es schon.

## 6. Offene Fragen an den Product Owner

1. Welche Option für die Ablage: A, B, C oder D?
2. PST: SFTP-Ordner, Upload in Teilen oder nur Desktop?
3. Darf die Entra-App die delegierte Berechtigung `Mail.Read` bekommen, und wer erteilt die Zustimmung im Mandanten?
