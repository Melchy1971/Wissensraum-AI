"""Read-only startup diagnostics with redacted, structured results."""
from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from migrations import CURRENT_SCHEMA_VERSION


@dataclass(frozen=True)
class DiagnosticResult:
    check_id: str
    status: str
    message: str
    details: Mapping[str, Any]


def check_python(version_info=None):
    version_info = version_info or __import__('sys').version_info
    version = '.'.join(str(part) for part in version_info[:3])
    major_minor = tuple(version_info[:2])
    release_level = getattr(version_info, 'releaselevel', version_info[3] if len(version_info) > 3 else 'final')
    if (3, 11) <= major_minor <= (3, 13) and release_level == 'final':
        return DiagnosticResult('python', 'PASS', f'Python {major_minor[0]}.{major_minor[1]} wird unterstützt.',
                                {'version': version})
    return DiagnosticResult('python', 'FAIL', 'Nicht unterstützte Python-Version; Python 3.11 bis 3.13 installieren.',
                            {'version': version, 'supported': '3.11–3.13'})


def check_tcl_tk(loader=None):
    try:
        if loader is None:
            import tkinter as tkinter_module
        else:
            tkinter_module = loader()
        version = str(tkinter_module.Tcl().call('info', 'patchlevel'))
        if not getattr(tkinter_module, 'TkVersion', None):
            raise RuntimeError('Tk metadata unavailable')
        return DiagnosticResult('tcl_tk', 'PASS', 'Tcl/Tk ist verfügbar.', {'version': version})
    except Exception:
        return DiagnosticResult('tcl_tk', 'FAIL', 'Tcl/Tk konnte nicht geladen werden; Python mit Tcl/Tk installieren.', {})


def check_pillow(loader=None, imagetk_loader=None):
    try:
        if loader is None:
            from PIL import Image as image_module
        else:
            image_module = loader()
        if imagetk_loader is None:
            from PIL import ImageTk as imagetk_module
        else:
            imagetk_module = imagetk_loader()
        if not getattr(imagetk_module, 'PhotoImage', None):
            raise RuntimeError('Pillow Tk adapter unavailable')
        image_module.new('RGB', (1, 1))
        version = str(getattr(image_module, '__version__', 'unbekannt'))
        return DiagnosticResult('pillow', 'PASS', 'Pillow ist verfügbar.', {'version': version})
    except Exception:
        return DiagnosticResult('pillow', 'FAIL', 'Pillow konnte nicht geladen werden; Abhängigkeiten neu installieren.', {})


def check_keyring(vault):
    try:
        backend = vault.backend()
        module = type(backend).__module__
        name = module.rsplit('.', 1)[-1]
        probe_account = '.wissensraum-diagnostics-' + uuid.uuid4().hex
        backend.get_password(getattr(vault, 'service', 'Wissensraum-AI'), probe_account)
        return DiagnosticResult('keyring', 'PASS', 'Ein sicherer Betriebssystem-Schlüsselspeicher ist verfügbar.',
                                {'backend': name})
    except Exception:
        return DiagnosticResult('keyring', 'FAIL', 'Kein sicherer Betriebssystem-Schlüsselspeicher verfügbar.', {})


def check_active_database(database_settings, sqlite_path, force_sqlite=False):
    try:
        backend = 'sqlite' if force_sqlite else database_settings.profile()['backend']
    except Exception:
        return DiagnosticResult('database', 'FAIL',
                                'Aktive Datenbankkonfiguration nicht lesbar; Konfiguration prüfen.',
                                {'backend': 'Unknown'})
    label = 'PostgreSQL' if backend == 'postgresql' else 'SQLite'
    details = {'backend': label}
    try:
        health = database_settings.health(Path(sqlite_path), force_sqlite=force_sqlite)
    except Exception:
        action = ('Verbindung, TLS, Rechte und Anwendungsschema prüfen.' if backend == 'postgresql' else
                  'Datei, Ordnerrechte und Anwendungsschema prüfen.')
        return DiagnosticResult('database', 'FAIL',
                                f'Aktive {label}-Datenbank nicht betriebsbereit; {action}', details)
    if health.get('missing'):
        return DiagnosticResult('database', 'WARN',
                                'SQLite-Datenbank ist noch nicht angelegt; sie wird beim ersten Start erstellt.',
                                details)
    if health.get('migration_needed'):
        return DiagnosticResult('database', 'WARN',
                                f'SQLite-Datenbank ist kompatibel und wird beim Start auf Schema '
                                f'{CURRENT_SCHEMA_VERSION} aktualisiert.',
                                details)
    return DiagnosticResult('database', 'PASS', f'Aktive {label}-Datenbank ist betriebsbereit.', details)


def check_data_directory(directory):
    directory = Path(directory)
    existed = directory.exists()
    owned_directories = []
    cleanup_failed = False
    try:
        if existed:
            if not directory.is_dir():
                raise OSError('not a directory')
            probe_root = directory / ('.wissensraum-diagnose-' + uuid.uuid4().hex)
            probe_root.mkdir()
            owned_directories.append(probe_root)
        else:
            if os.path.lexists(directory):
                raise OSError('unusable directory path')
            ancestor = directory.parent
            missing_directories = [directory]
            while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
                missing_directories.append(ancestor)
                ancestor = ancestor.parent
            if not ancestor.is_dir():
                raise OSError('not a directory')
            probe_root = ancestor / ('.wissensraum-diagnose-' + uuid.uuid4().hex)
            probe_root.mkdir()
            owned_directories.append(probe_root)
            probe_directory = probe_root
            for missing in reversed(missing_directories):
                probe_directory = probe_directory / missing.name
                probe_directory.mkdir()
                owned_directories.append(probe_directory)
        message = ('Das Anwendungsdatenverzeichnis ist beschreibbar.' if existed else
                   'Das Anwendungsdatenverzeichnis kann beim ersten Start angelegt werden.')
    except Exception:
        return DiagnosticResult('data_directory', 'FAIL',
                                'Anwendungsdatenverzeichnis nicht beschreibbar; Ordnerrechte prüfen.',
                                {'directory': directory.name})
    finally:
        for owned in reversed(owned_directories):
            try: owned.rmdir()
            except OSError: cleanup_failed = True
    if cleanup_failed:
        return DiagnosticResult('data_directory', 'FAIL',
                                'Anwendungsdatenverzeichnis nicht beschreibbar; Ordnerrechte prüfen.',
                                {'directory': directory.name})
    if not existed and os.path.lexists(directory) and not directory.is_dir():
        return DiagnosticResult('data_directory', 'FAIL',
                                'Anwendungsdatenverzeichnis nicht beschreibbar; Ordnerrechte prüfen.',
                                {'directory': directory.name})
    return DiagnosticResult('data_directory', 'PASS', message,
                            {'directory': directory.name})


def run_diagnostics(base=None, vault=None, database_settings=None, sqlite_path=None, checks=None,
                    force_sqlite=False):
    if checks is None:
        data_directory = Path(base) / 'data'
        checks = (
            check_python,
            check_tcl_tk,
            check_pillow,
            lambda: check_keyring(vault),
            lambda: check_active_database(database_settings, sqlite_path or data_directory / 'wissen.db',
                                          force_sqlite=force_sqlite),
            lambda: check_data_directory(data_directory),
        )
    return tuple(check() for check in checks)


def check_store_health(store):
    label = 'PostgreSQL' if getattr(store, 'backend', '') == 'postgresql' else 'SQLite'
    try:
        with store.connect() as db:
            row = db.execute('SELECT count(*) AS total FROM documents').fetchone()
        total = row['total'] if hasattr(row, 'keys') else row[0]
    except Exception:
        return DiagnosticResult('database', 'FAIL',
                                f'Aktive {label}-Datenbank antwortet nicht; Verbindung und Rechte prüfen.',
                                {'backend': label})
    return DiagnosticResult('database', 'PASS', f'Aktive {label}-Datenbank antwortet.',
                            {'backend': label, 'documents': total})


def check_ai_provider(settings, providers):
    try:
        provider = settings.active()
        _, source = settings.credential(provider)
        details = {'provider': providers[provider]['name'], 'model': settings.model(provider),
                   'source': source}
    except Exception:
        return DiagnosticResult('ai_provider', 'FAIL',
                                'KI-Einstellungen nicht lesbar; Einstellungen und Schlüsselspeicher prüfen.', {})
    if source in ('Nicht hinterlegt', 'Deaktiviert'):
        return DiagnosticResult('ai_provider', 'WARN',
                                'Aktive KI ist nicht nutzbar; unter Einstellungen einen Schlüssel hinterlegen '
                                'oder einen anderen Anbieter aktivieren.', details)
    return DiagnosticResult('ai_provider', 'PASS',
                            'Aktive KI ist konfiguriert; die Erreichbarkeit prüft „Verbindung testen“.', details)


def check_mail_accounts(mail_accounts):
    try:
        accounts = mail_accounts.list()
    except Exception:
        return DiagnosticResult('mail_accounts', 'FAIL',
                                'E-Mail-Konten nicht lesbar; Datenbank und Schlüsselspeicher prüfen.', {})
    if not accounts:
        return DiagnosticResult('mail_accounts', 'PASS', 'Kein E-Mail-Konto eingerichtet.', {'accounts': 0})
    missing = [account['label'] for account in accounts if account['auth_status'] != 'Verbunden']
    details = {'accounts': len(accounts)}
    if missing:
        return DiagnosticResult('mail_accounts', 'WARN',
                                f'{len(missing)} von {len(accounts)} E-Mail-Konten ohne gespeicherte Zugangsdaten: '
                                + ', '.join(missing) + '.', details)
    return DiagnosticResult('mail_accounts', 'PASS',
                            'Alle E-Mail-Konten haben gespeicherte Zugangsdaten.', details)


def health_overview(store, settings, mail_accounts, providers):
    """Read local state only; never contacts AI providers or mail servers."""
    return (check_store_health(store), check_ai_provider(settings, providers),
            check_mail_accounts(mail_accounts))


def format_diagnostics(results):
    results = tuple(results)
    lines = []
    for result in results:
        details = ', '.join(f'{key}={result.details[key]}' for key in sorted(result.details))
        suffix = f' ({details})' if details else ''
        lines.append(f'[{result.status}] {result.check_id}: {result.message}{suffix}')
    counts = {status: sum(result.status == status for result in results) for status in ('PASS', 'WARN', 'FAIL')}
    lines.append(f'Ergebnis: {counts["PASS"]} bestanden, {counts["WARN"]} Warnungen, '
                 f'{counts["FAIL"]} fehlgeschlagen.')
    return '\n'.join(lines)
