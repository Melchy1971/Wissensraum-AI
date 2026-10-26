"""Read-only startup diagnostics with redacted, structured results."""
from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def check_active_database(database_settings, sqlite_path, force_sqlite=False, store=None):
    try:
        backend = 'sqlite' if force_sqlite else database_settings.profile()['backend']
    except Exception:
        return DiagnosticResult('database', 'FAIL',
                                'Aktive Datenbankkonfiguration nicht lesbar; Konfiguration prüfen.',
                                {'backend': 'Unknown'})
    label = 'PostgreSQL' if backend == 'postgresql' else 'SQLite'
    details = {'backend': label}
    if store is not None:
        details['active_provider'] = getattr(store, 'backend', 'unknown')
        try:
            with store.connect() as db:
                details['email_accounts'] = db.execute('SELECT COUNT(*) AS count FROM email_accounts').fetchone()['count']
        except Exception:
            details['email_accounts'] = 'unavailable'
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
                                'SQLite-Datenbank ist kompatibel und wird beim Start auf Schema 4 aktualisiert.',
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
