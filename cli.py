"""Command-line interface for automation and headless use."""
import argparse
import json
from pathlib import Path
from core import Store, import_document
from migrations import restore_sqlite
from settings import PROVIDERS, Settings
from settings import SystemVault
from database import DatabaseSettings, resolve_sqlite_path
from configuration_authority import ConfigurationAuthority, identity_for_profile
from diagnostics import DiagnosticResult, format_diagnostics, health_overview, run_diagnostics
from importer import MailAccounts
from import_jobs import ImportJobs
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

def main(argv=None):
    parser = argparse.ArgumentParser(description='Wissensraum: SQL-Wissensmanagement')
    parser.add_argument('--db',type=Path,help='Lokale SQLite-Datei; überschreibt die konfigurierte Datenquelle')
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('doctor')
    commands.add_parser('status',help='Gesundheit von Datenbank, aktiver KI und E-Mail-Konten anzeigen')
    commands.add_parser('config-sync')
    commands.add_parser('list')
    imp = commands.add_parser('import')
    imp.add_argument('path',type=Path)
    imp.add_argument('--category',default='Allgemein')
    folder_import = commands.add_parser('import-folder',help='Ordner inkl. Unterordner als Importauftrag einlesen')
    folder_import.add_argument('folder',type=Path)
    folder_import.add_argument('--category',default='Allgemein')
    commands.add_parser('jobs',help='Letzte Importaufträge mit Zählern anzeigen')
    job_resume = commands.add_parser('job-resume',help='Offenen oder abgebrochenen Importauftrag fortsetzen')
    job_resume.add_argument('job_id',type=int)
    job_report = commands.add_parser('job-report',help='Importbericht als CSV schreiben')
    job_report.add_argument('job_id',type=int)
    job_report.add_argument('target',type=Path)
    for name in ('search','ask'):
        sub = commands.add_parser(name)
        sub.add_argument('query')
    summary = commands.add_parser('summarize')
    summary.add_argument('id',type=int)
    backup = commands.add_parser('backup')
    backup.add_argument('target',type=Path)
    restore = commands.add_parser('restore',help='SQLite-Datenbank aus einer Sicherung wiederherstellen')
    restore.add_argument('source',type=Path)
    restore.add_argument('--confirm',default='',help='Dateiname der Zieldatenbank zur Bestätigung')
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
                result = store.list()
            elif args.command == 'config-sync':
                ConfigurationAuthority(BASE).synchronize(store)
                result = {'status':'synchronisiert','restart_required':False}
            elif args.command == 'import':
                title,text,name,data = import_document(args.path)
                doc_id,created = store.add(title,text,args.category,name,data)
                result = {'id':doc_id,'created':created}
            elif args.command == 'import-folder':
                jobs = ImportJobs(store)
                result = jobs.run(jobs.create_for_folder(args.folder,args.category))
            elif args.command == 'jobs':
                result = ImportJobs(store).jobs()
            elif args.command == 'job-resume':
                result = ImportJobs(store).run(args.job_id)
            elif args.command == 'job-report':
                result = {'bericht':str(ImportJobs(store).write_report(args.job_id,args.target))}
            elif args.command == 'status':
                results = health_overview(store,Settings(store,BASE,vault),
                                          MailAccounts(store,vault,BASE),PROVIDERS)
                print(format_diagnostics(results))
                _log_event(logger,'command_completed',metadata=metadata)
                return 1 if any(result.status == 'FAIL' for result in results) else 0
            elif args.command == 'search':
                result = store.search(args.query)
            elif args.command == 'backup':
                store.backup(args.target)
                result = {'backup':str(args.target)}
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
