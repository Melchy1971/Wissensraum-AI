"""Command-line interface for automation and headless use."""
import argparse
import json
from pathlib import Path
from core import Store, discover_documents, import_documents, import_document
from settings import Settings
from settings import SystemVault
from database import DatabaseSettings, resolve_sqlite_path
from configuration_authority import ConfigurationAuthority, identity_for_profile
from diagnostics import DiagnosticResult, format_diagnostics, health_overview, run_diagnostics
from importer import Importer, MailAccounts
from import_jobs import ImportJobs
import mail_cleanup
import rules
from configuration import (APPLY_ERROR, EXPORT_ERROR, ConfigurationTransfer,
                           ConfigurationTransferError, RECOVERY_ERROR,
                           pending_configuration_recovery,
                           recover_pending_configuration)
from design import DesignSettings
from local_logging import (create_operational_logger, known_runtime_secrets,
                           safe_exception_class, warn_operational_logging_failure)
BASE = Path(__file__).resolve().parent


def _log_event(logger,event_name,**kwargs):
    if logger is None:
        return
    try:
        logger.event(event_name,**kwargs)
    except BaseException:
        warn_operational_logging_failure()


def _close_logger(logger):
    if logger is None:
        return
    try:
        logger.close()
    except BaseException:
        warn_operational_logging_failure()


def _write_import_report(path, report, format_):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if format_ == 'csv':
        lines = ['field,value']
        for key in ('found', 'supported', 'imported', 'duplicates', 'skipped', 'errors'):
            if key in report:
                lines.append(f'{key},{report[key]}')
        details = report.get('details', [])
        if details:
            escaped = ' | '.join(str(item).replace('"', '""') for item in details)
            lines.append(f'details,"{escaped}"')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return
    lines = [
        '# Importbericht',
        '',
        f"- Gefunden: {report.get('found', 0)}",
        f"- Unterstützt: {report.get('supported', 0)}",
        f"- Importiert: {report.get('imported', 0)}",
        f"- Duplikate: {report.get('duplicates', 0)}",
        f"- Übersprungen: {report.get('skipped', 0)}",
        f"- Fehler: {report.get('errors', 0)}",
    ]
    details = report.get('details', [])
    if details:
        lines += ['', '## Details'] + [f'- {item}' for item in details]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Wissensraum: SQL-Wissensmanagement')
    parser.add_argument('--db',type=Path,help='Lokale SQLite-Datei; überschreibt die konfigurierte Datenquelle')
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('doctor')
    commands.add_parser('status',help='Gesundheit von Datenbank, aktiver KI und E-Mail-Konten anzeigen')
    commands.add_parser('config-sync')
    listing = commands.add_parser('list')
    listing.add_argument('--all-versions',action='store_true',help='auch ältere Versionen anzeigen')
    listing.add_argument('--tag',help='nur Dokumente mit diesem Tag')
    commands.add_parser('tags',help='Alle Tags mit Anzahl der Dokumente')
    commands.add_parser('rules',help='Regeln für Bereich und Tags in ihrer Reihenfolge anzeigen')
    rule_add = commands.add_parser('rule-add',help='Regel anlegen (mindestens eine Bedingung, Bereich oder Tags)')
    rule_add.add_argument('name')
    for option in ('source-type','path-pattern','sender-domain','extension','keyword','category','tags'):
        rule_add.add_argument('--'+option,default='')
    rule_delete = commands.add_parser('rule-delete',help='Regel löschen; gesetzte Bereiche und Tags bleiben')
    rule_delete.add_argument('rule_id',type=int)
    commands.add_parser('rules-preview',help='Zeigen, was die Regeln an vorhandenen Dokumenten ändern würden')
    rules_apply = commands.add_parser('rules-apply',help='Regeln auf vorhandene Dokumente anwenden (als Lauf)')
    rules_apply.add_argument('--confirm',action='store_true',help='Änderungen wirklich übernehmen')
    commands.add_parser('rules-runs',help='Angewendete Läufe anzeigen')
    rules_revert = commands.add_parser('rules-revert',help='Lauf vollständig zurücknehmen')
    rules_revert.add_argument('run_id',type=int)
    versions = commands.add_parser('versions',help='Alle Versionen eines Dokuments anzeigen')
    versions.add_argument('id',type=int)
    strip = commands.add_parser('strip-mail-attachments',
                                help='Anhänge aus vor Schritt 6.3a gespeicherten IMAP-Originalen entfernen')
    strip.add_argument('--dry-run',action='store_true',help='nur zählen, nichts ändern')
    strip.add_argument('--backup-confirmed',action='store_true',
                       help='PostgreSQL: eine aktuelle pg_dump-Sicherung liegt vor')
    commands.add_parser('ocr-status',help='Optionale Texterkennung: Tesseract-Version und Sprachen')
    commands.add_parser('retry-no-text',help='Quarantäne „kein lesbarer Text“ erneut verarbeiten (z. B. nach OCR-Installation)')
    preview = commands.add_parser('preview',help='Datei oder Ordner prüfen, ohne etwas zu speichern')
    preview.add_argument('path',type=Path)
    imp = commands.add_parser('import')
    imp.add_argument('path',type=Path)
    imp.add_argument('--category',default='Allgemein')
    imp_folder = commands.add_parser('import-folder')
    imp_folder.add_argument('path',type=Path)
    imp_folder.add_argument('--report',type=Path)
    imp_folder.add_argument('--report-format',choices=('markdown','csv'),default='markdown')
    for name in ('search','ask'):
        sub = commands.add_parser(name)
        sub.add_argument('query')
        if name == 'search':
            sub.add_argument('--tag',help='nur Dokumente mit diesem Tag')
    summary = commands.add_parser('summarize')
    summary.add_argument('id',type=int)
    backup = commands.add_parser('backup')
    backup.add_argument('target',type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('source',type=Path)
    config_export = commands.add_parser('config-export')
    config_export.add_argument('target',type=Path)
    config_import = commands.add_parser('config-import')
    config_import.add_argument('source',type=Path)
    args = parser.parse_args(argv)
    try:
        vault = SystemVault()
        database_settings = DatabaseSettings(BASE,vault)
        # Secret-free export resolves SQLite only from its durable local
        # identity (or explicit --db), never from the process environment.
        sqlite_path = (args.db if args.command == 'config-export' else
                       resolve_sqlite_path(BASE,explicit=args.db))
        recovery_pending = pending_configuration_recovery(BASE)
        if args.command == 'config-export' and recovery_pending:
            raise ConfigurationTransferError(RECOVERY_ERROR,'CONFIG_IMPORT_RECOVERY_INCOMPLETE')
        if args.command == 'doctor':
            results = run_diagnostics(BASE,vault,database_settings,sqlite_path,
                                      force_sqlite=args.db is not None)
            if recovery_pending:
                results = (*results, DiagnosticResult(
                    'configuration_recovery', 'FAIL',
                    'Konfigurationswiederherstellung ist ausstehend; Anwendung normal starten.', {}))
            print(format_diagnostics(results))
            return 1 if any(result.status == 'FAIL' for result in results) else 0
        if args.command == 'restore':
            if recovery_pending:
                raise ConfigurationTransferError(RECOVERY_ERROR,'CONFIG_IMPORT_RECOVERY_INCOMPLETE')
            if args.db is None and database_settings.profile()['backend'] != 'sqlite':
                raise ValueError('Wiederherstellung ist nur für SQLite verfügbar; '
                                 'PostgreSQL wird auf dem Server mit pg_restore wiederhergestellt.')
            if args.confirm != sqlite_path.name:
                raise ValueError(f'Bestätigung erforderlich: --confirm {sqlite_path.name}')
            logger = create_operational_logger(BASE,'cli')
            metadata={'operation':args.command,'backend':'sqlite'}
            _log_event(logger,'command_started',metadata=metadata)
            try:
                result = restore_sqlite(sqlite_path,args.source)
                if args.db is None:
                    # Restored settings and accounts replace the cached authority.
                    ConfigurationAuthority(BASE).synchronize(Store(sqlite_path))
                print(json.dumps(result,ensure_ascii=False,indent=2))
                _log_event(logger,'command_completed',metadata=metadata)
                return 0
            except Exception as error:
                _log_event(logger,'command_failed',severity='ERROR',metadata=metadata,
                           exception_class=safe_exception_class(error),error_code='CLI_COMMAND_FAILED')
                raise
            finally:
                _close_logger(logger)
        if args.command != 'config-export':
            recover_pending_configuration(BASE,database_settings,sqlite_path)
        if args.command == 'config-export':
            profile=database_settings.persisted_profile()
            expected=identity_for_profile(
                {'backend':'sqlite'} if args.db is not None else profile,
                args.db or sqlite_path,base=BASE)
            logger = create_operational_logger(BASE,'cli')
            metadata={'operation':args.command,'backend':expected['backend']}
            _log_event(logger,'command_started',metadata=metadata)
            try:
                transfer=ConfigurationTransfer(None,BASE,database_settings,DesignSettings(BASE),
                                               expected_store_identity=expected)
                try:
                    transfer.export_file(args.target)
                except ConfigurationTransferError:
                    raise
                except Exception:
                    raise ConfigurationTransferError(EXPORT_ERROR,'CONFIG_EXPORT_FAILED') from None
                print(json.dumps({'status':'exportiert','restart_required':False},ensure_ascii=False,indent=2))
                _log_event(logger,'command_completed',metadata=metadata)
                return 0
            finally:
                _close_logger(logger)
        store = Store(args.db) if args.db else database_settings.open_store(sqlite_path)
        logger = create_operational_logger(
            BASE,'cli',known_secrets=known_runtime_secrets(store))
        metadata={'operation':args.command,'backend':store.backend}
        _log_event(logger,'command_started',metadata=metadata)
        try:
            if args.command == 'list':
                result = store.list(all_versions=args.all_versions,tag=args.tag)
            elif args.command in RULE_COMMANDS:
                result = rule_command(store,args)
            elif args.command == 'ocr-status':
                import ocr
                result = ocr.status()
            elif args.command == 'retry-no-text':
                jobs = ImportJobs(store,Importer(store,MailAccounts(store,vault,BASE)))
                result = [jobs.run(job_id) for job_id in jobs.retry_quarantined('no_text')]
            elif args.command == 'strip-mail-attachments':
                result = mail_cleanup.strip_stored_attachments(store,dry_run=args.dry_run,
                                                               backup_confirmed=args.backup_confirmed)
            elif args.command == 'versions':
                result = store.versions(args.id)
            elif args.command == 'preview':
                jobs = ImportJobs(store)
                result = (jobs.preview_folder(args.path) if args.path.is_dir()
                          else jobs.preview_files([args.path]))
            elif args.command == 'config-sync':
                ConfigurationAuthority(BASE).synchronize(store)
                result = {'status':'synchronisiert','restart_required':False}
            elif args.command == 'import':
                title,text,name,data = import_document(args.path)
                doc_id,created = store.add(title,text,args.category,name,data)
                result = {'id':doc_id,'created':created}
            elif args.command == 'import-folder':
                documents, skipped, errors = discover_documents(args.path)
                report = import_documents(store, documents, skipped, errors)
                result = report
                if args.report:
                    _write_import_report(args.report, report, args.report_format)
            elif args.command == 'search':
                result = store.search(args.query,tag=args.tag)
            elif args.command == 'backup':
                store.backup(args.target)
                result = {'backup':str(args.target)}
            elif args.command == 'restore':
                store.restore(args.source)
                result = {'restored_from':str(args.source)}
            elif args.command == 'config-import':
                transfer=ConfigurationTransfer(store,BASE,database_settings,DesignSettings(BASE))
                try:
                    imported=transfer.import_file(args.source)
                except ConfigurationTransferError:
                    raise
                except Exception:
                    raise ConfigurationTransferError(APPLY_ERROR,'CONFIG_IMPORT_FAILED') from None
                result={'status':'importiert','restart_required':imported['restart_required']}
                if imported['restart_required']:
                    result['hinweis']='Programm neu starten, damit die konfigurierte Datenquelle verwendet wird.'
            else:
                ai = Settings(store,BASE).ai()
                answer,sources = ai.ask(args.query) if args.command=='ask' else ai.summarize(args.id)
                result = {'answer':answer,'sources':sources}
            print(json.dumps(result,ensure_ascii=False,indent=2))
            _log_event(logger,'command_completed',metadata=metadata)
            return 0
        except Exception as error:
            _log_event(logger,'command_failed',severity='ERROR',metadata=metadata,
                       exception_class=safe_exception_class(error),error_code='CLI_COMMAND_FAILED')
            raise
        finally:
            _close_logger(logger)
    except Exception as e:
        if args.command == 'config-export':
            message = str(e) if isinstance(e, ConfigurationTransferError) else EXPORT_ERROR
        elif args.command == 'config-import':
            message = str(e) if isinstance(e, ConfigurationTransferError) else APPLY_ERROR
        else:
            message = str(e)
        parser.exit(1,f'Fehler: {message}\n')

if __name__ == '__main__':
    raise SystemExit(main())
