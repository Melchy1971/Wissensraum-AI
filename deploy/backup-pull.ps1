# Holt die verschlüsselten Sicherungen vom VPS auf einen Windows-Rechner (Paritäts-Roadmap Phase 0, S-03).
# Gegenstück zu backup-pull.sh für Linux. Läuft über WireGuard mit dem eingebauten OpenSSH von Windows.
#
# Der VPS braucht keinen Zugang zu diesem Rechner. Auf dem VPS darf der Benutzer wrbackup nur den
# Sicherungsordner lesen (deploy/backup-setup.sh). Übertragen werden nur fertige Dateien, keine *.tmp,
# und nur solche, die hier noch fehlen. Danach werden alle Prüfsummen geprüft.
#
# Aufruf:            powershell -ExecutionPolicy Bypass -File deploy\backup-pull.ps1
# Täglich einplanen: powershell -ExecutionPolicy Bypass -File deploy\backup-pull.ps1 -Register
# Exitcode 1, wenn eine Prüfsumme nicht stimmt, keine Sicherung da ist oder die neueste älter als 36 Stunden ist.
param(
    [string]$Vps = 'wrbackup@10.13.13.1',
    [string]$RemoteDir = '/var/backups/wissensraum',
    [string]$Dest = (Join-Path $HOME 'Wissensraum-Sicherung\sicherungen'),
    [string]$SshKey = (Join-Path $HOME 'Wissensraum-Sicherung\wrbackup_ed25519'),
    [int]$KeepDays = 90,
    [int]$MaxAgeHours = 36,
    [switch]$Register,
    [string]$At = '12:00'
)
$ErrorActionPreference = 'Stop'

if ($Register) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    # Verpasste Läufe nachholen, wenn der Rechner zur geplanten Zeit aus war.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
    Register-ScheduledTask -TaskName 'Wissensraum Sicherung abholen' -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Holt die verschlüsselten Sicherungen vom VPS (backup-pull.ps1).' -Force | Out-Null
    Write-Output "Aufgabe 'Wissensraum Sicherung abholen' täglich um $At eingeplant."
    exit 0
}

$ssh = @('-i', $SshKey, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new')
New-Item -ItemType Directory -Force $Dest | Out-Null

# Nur einfache Dateinamen aus dem Sicherungsordner, keine Pfade und keine halbfertigen Dateien.
$remote = & ssh @ssh $Vps "ls -1 '$RemoteDir'"
if ($LASTEXITCODE -ne 0) { throw "Verbindung zu $Vps fehlgeschlagen (Exitcode $LASTEXITCODE)." }
$names = @($remote | Where-Object { $_ -match '^[A-Za-z0-9._-]+$' -and $_ -notlike '*.tmp' })
foreach ($name in $names) {
    $local = Join-Path $Dest $name
    # restore-drill.log wächst mit jeder Übung, deshalb immer neu holen.
    if ((Test-Path $local) -and $name -ne 'restore-drill.log') { continue }
    & scp @ssh -q "${Vps}:$RemoteDir/$name" $local
    if ($LASTEXITCODE -ne 0) { throw "Übertragung von $name fehlgeschlagen." }
}

$failed = $false
foreach ($sums in Get-ChildItem $Dest -Filter '*.sha256') {
    foreach ($line in Get-Content $sums.FullName) {
        if ($line -notmatch '^([0-9a-f]{64}) [ *](\S+)$') { continue }
        $file = Join-Path $Dest $Matches[2]
        if (-not (Test-Path $file)) { Write-Error "Fehlt: $($Matches[2])" -ErrorAction Continue; $failed = $true; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $Matches[1]) {
            Write-Error "Prüfsumme falsch: $($Matches[2])" -ErrorAction Continue; $failed = $true
        }
    }
}

$limit = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $Dest -Filter 'wissensraum_server-*' | Where-Object { $_.LastWriteTime -lt $limit } | Remove-Item

# Der Zeitstempel steht im Namen (UTC), so zählt das Alter der Sicherung, nicht das der Übertragung.
$newest = Get-ChildItem $Dest -Filter 'wissensraum_server-*.dump.age' | Sort-Object Name | Select-Object -Last 1
if (-not $newest) { Write-Error 'Keine Sicherung vorhanden.' -ErrorAction Continue; exit 1 }
$stamp = [datetime]::ParseExact(($newest.Name -replace '^wissensraum_server-(\d{8}T\d{6}Z)\.dump\.age$', '$1'),
    "yyyyMMdd'T'HHmmss'Z'", [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]'AssumeUniversal,AdjustToUniversal')
$hours = [int][math]::Floor(((Get-Date).ToUniversalTime() - $stamp).TotalHours)
Write-Output "Neueste Sicherung: $($newest.Name), vor $hours Stunden."
if ($hours -gt $MaxAgeHours) { Write-Error "Neueste Sicherung ist älter als $MaxAgeHours Stunden." -ErrorAction Continue; $failed = $true }
if ($failed) { exit 1 }
exit 0
