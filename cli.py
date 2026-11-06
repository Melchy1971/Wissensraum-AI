"""Command-line interface for automation and headless use."""
import argparse
import json
import sys
from pathlib import Path
from core import DOCUMENT_STATUSES, RELATION_KINDS, Store, import_document
from migrations import restore_sqlite
from settings import PROVIDERS, Settings
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

RULE_COMMANDS = ('tags','rules','rule-add','rule-delete','rules-preview','rules-apply','rules-runs','rules-revert')


def rule_command(store,args):
    """Rules and tags (step S2); existing documents change only with rules-apply --confirm."""
    with store.connect() as db:
        if args.command == 'tags':
            return [{'tag':name,'documents':count} for name,count in rules.tag_counts(db)]
        if args.command == 'rules':
            return rules.list_rules(db)
        if args.command == 'rule-add':
            fields = {name:getattr(args,name) for name in ('name','source_type','path_pattern','sender_domain',
                                                           'extension','keyword','category','tags')}
            return {'rule_id':rules.save_rule(db,fields)}
        if args.command == 'rule-delete':
            rules.delete_rule(db,args.rule_id)
            return {'deleted':args.rule_id}
        if args.command == 'rules-runs':
            return rules.runs(db)
        if args.command == 'rules-revert':
            return rules.revert(db,store.backend,args.run_id)
        rows = rules.preview(db)
        if args.command == 'rules-preview' or not args.confirm:
            return {'documents':len(rows),'changes':rows,
                    'hint':'Nichts geändert. Mit rules-apply --confirm übernehmen.'}
        return rules.apply(db,store.backend,rows)


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
    listing.add_argument('--collection',type=int,help='nur Dokumente dieser Sammlung')
    listing.add_argument('--favorites',action='store_true',help='nur Favoriten')
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
    edit = commands.add_parser('edit',help='Titel, Bereich, Tags, Status, Verantwortlichen oder Wiedervorlage ändern '
                                            '(Original bleibt unverändert)')
    edit.add_argument('id',type=int)
    edit.add_argument('--title')
    edit.add_argument('--category')
    edit.add_argument('--add-tags',help='kommagetrennt')
    edit.add_argument('--remove-tags',help='kommagetrennt')
    edit.add_argument('--status',choices=DOCUMENT_STATUSES)
    edit.add_argument('--owner',help='Verantwortlicher, leer entfernt ihn')
    edit.add_argument('--review-date',help='Wiedervorlage JJJJ-MM-TT, leer entfernt sie')
    due = commands.add_parser('review-due',help='Dokumente mit fälliger Wiedervorlage')
    due.add_argument('--until',help='Stichtag JJJJ-MM-TT, Standard heute')
    commands.add_parser('collections',help='Sammlungen mit Anzahl der Dokumente')
    for name,help_text in (('collection-create','Sammlung anlegen'),):
        commands.add_parser(name,help=help_text).add_argument('name')
    rename = commands.add_parser('collection-rename',help='Sammlung umbenennen')
    rename.add_argument('id',type=int); rename.add_argument('name')
    commands.add_parser('collection-delete',help='Sammlung löschen (Dokumente bleiben erhalten)').add_argument('id',type=int)
    for name,help_text in (('collection-add','Dokumente in eine Sammlung aufnehmen'),
                           ('collection-remove','Dokumente aus einer Sammlung entfernen')):
        member = commands.add_parser(name,help=help_text)
        member.add_argument('id',type=int); member.add_argument('documents',type=int,nargs='+')
    export = commands.add_parser('collection-export',help='Sammlung als ZIP mit Originalen und Quellenmanifest')
    export.add_argument('id',type=int); export.add_argument('target',type=Path)
    export.add_argument('--overwrite',action='store_true',help='vorhandene Zieldatei ersetzen')
    favorite = commands.add_parser('favorite',help='Dokument als Favorit markieren')
    favorite.add_argument('id',type=int); favorite.add_argument('--remove',action='store_true')
    commands.add_parser('searches',help='Gespeicherte Suchen')
    save = commands.add_parser('search-save',help='Suche speichern (gleicher Name ersetzt sie)')
    save.add_argument('name'); save.add_argument('query')
    save.add_argument('--mode',choices=('fulltext','vector','hybrid')); save.add_argument('--tag')
    commands.add_parser('search-delete',help='Gespeicherte Suche löschen').add_argument('id',type=int)
    relate = commands.add_parser('relate',help='Beziehung zwischen zwei Dokumenten anlegen')
    relate.add_argument('source',type=int); relate.add_argument('target',type=int)
    relate.add_argument('--kind',required=True,choices=RELATION_KINDS); relate.add_argument('--note',default='')
    commands.add_parser('unrelate',help='Beziehung entfernen').add_argument('id',type=int)
    commands.add_parser('relations',help='Beziehungen eines Dokuments').add_argument('id',type=int)
    compare = commands.add_parser('compare',help='Zwei Fassungen zeilenweise vergleichen (alt, neu)')
    compare.add_argument('old',type=int); compare.add_argument('new',type=int)
    retired = commands.add_parser('ai-retired',help='Veraltete und archivierte Dokumente in KI-Antworten')
    retired.add_argument('mode',nargs='?',choices=('exclude','include'),help='ohne Angabe: aktuelle Einstellung')
    metadata = commands.add_parser('history',help='Verlauf der Metadatenänderungen eines Dokuments')
    metadata.add_argument('id',type=int)
    versions = commands.add_parser('versions',help='Alle Versionen eines Dokuments anzeigen')
    versions.add_argument('id',type=int)
    strip = commands.add_parser('strip-mail-attachments',
                                help='Anhänge aus vor Schritt 6.3a gespeicherten IMAP-Originalen entfernen')
    strip.add_argument('--dry-run',action='store_true',help='nur zählen, nichts ändern')
    strip.add_argument('--backup-confirmed',action='store_true',
                       help='PostgreSQL: eine aktuelle pg_dump-Sicherung liegt vor')
    commands.add_parser('ocr-status',help='Optionale Texterkennung: Tesseract-Version und Sprachen')
    vectors = commands.add_parser('vectors',help='Semantische Suche: Status, Aktivierung und Einbettung über lokales Ollama')
    vector_action = vectors.add_mutually_exclusive_group()
    vector_action.add_argument('--activate',action='store_true',help='aktivieren (prüft einmal das Ollama-Modell)')
    vector_action.add_argument('--deactivate',action='store_true')
    vector_action.add_argument('--run',action='store_true',help='alle Abschnitte ohne Vektor jetzt einbetten')
    vectors.add_argument('--model',help='Ollama-Modell beim Aktivieren (Standard qwen3-embedding:0.6b)')
    chunking = commands.add_parser('chunking',help='Abschnittslänge je Dokumenttyp anzeigen oder ändern '
                                   '(Standard 1.800 Zeichen, 200 Überlappung)')
    chunking.add_argument('type',nargs='?',help='Dokumenttyp: Dateiendung (pdf, docx, md, eml) oder Quelle '
                          '(note, pst, imap, graph)')
    chunking_action = chunking.add_mutually_exclusive_group()
    chunking_action.add_argument('--size',type=int,help='Zeichen je Abschnitt (300 bis 8.000)')
    chunking_action.add_argument('--reset',action='store_true',help='Typ auf den Standard zurücksetzen')
    chunking.add_argument('--overlap',type=int,default=200,help='Überlappung in Zeichen (Standard 200)')
    commands.add_parser('retry-no-text',help='Quarantäne „kein lesbarer Text“ erneut verarbeiten (z. B. nach OCR-Installation)')
    preview = commands.add_parser('preview',help='Datei oder Ordner prüfen, ohne etwas zu speichern')
    preview.add_argument('path',type=Path)
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
        if name == 'search':
            sub.add_argument('--tag',help='nur Dokumente mit diesem Tag')
            sub.add_argument('--mode',choices=('fulltext','vector','hybrid'),
                             help='Standard: hybrid, wenn die semantische Suche aktiv ist, sonst Volltext')
            sub.add_argument('--diagnose',action='store_true',help='Ränge, Rohwerte und Laufzeiten mit ausgeben')
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
            exit_code = 0
            if args.command == 'list':
                result = store.list(all_versions=args.all_versions,tag=args.tag,collection=args.collection,
                                    favorites=args.favorites)
            elif args.command == 'relate':
                result = {'id':store.relate(args.source,args.target,args.kind,args.note)}
            elif args.command == 'unrelate':
                store.unrelate(args.id); result = {'geloescht':args.id}
            elif args.command == 'relations':
                result = store.relations(args.id)
            elif args.command == 'compare':
                result = store.compare(args.old,args.new)
            elif args.command == 'collections':
                result = store.collections()
            elif args.command == 'collection-create':
                result = {'id':store.create_collection(args.name)}
            elif args.command == 'collection-rename':
                store.rename_collection(args.id,args.name); result = {'id':args.id}
            elif args.command == 'collection-delete':
                store.delete_collection(args.id); result = {'geloescht':args.id}
            elif args.command == 'collection-add':
                result = {'aufgenommen':store.add_to_collection(args.id,args.documents)}
            elif args.command == 'collection-remove':
                result = {'entfernt':store.remove_from_collection(args.id,args.documents)}
            elif args.command == 'collection-export':
                report = store.export_collection(args.id,args.target,args.overwrite)
                result = {'datei':str(args.target),'dokumente':len(report['documents'])}
            elif args.command == 'favorite':
                store.set_favorite(args.id,not args.remove); result = {'id':args.id,'favorit':not args.remove}
            elif args.command == 'searches':
                result = store.saved_searches()
            elif args.command == 'search-save':
                result = {'id':store.save_search(args.name,args.query,args.mode,args.tag)}
            elif args.command == 'search-delete':
                store.delete_saved_search(args.id); result = {'geloescht':args.id}
            elif args.command in RULE_COMMANDS:
                result = rule_command(store,args)
            elif args.command == 'ocr-status':
                import ocr
                result = ocr.status()
            elif args.command == 'vectors':
                result = vector_command(store,args)
            elif args.command == 'retry-no-text':
                jobs = ImportJobs(store,Importer(store,MailAccounts(store,vault,BASE)))
                result = [jobs.run(job_id) for job_id in jobs.retry_quarantined('no_text')]
            elif args.command == 'strip-mail-attachments':
                result = mail_cleanup.strip_stored_attachments(store,dry_run=args.dry_run,
                                                               backup_confirmed=args.backup_confirmed)
            elif args.command == 'chunking':
                from core import DEFAULT_CHUNKING
                if args.type and (args.size or args.reset):
                    changed = store.set_chunk_strategy(args.type,None if args.reset else args.size,args.overlap)
                    result = {'typ':args.type,'neu_zerlegt':changed,
                              'hinweis':'Neue Abschnitte werden bei aktiver semantischer Suche im Hintergrund '
                                        'eingebettet.' if changed else 'Keine Dokumente dieses Typs geändert.'}
                elif args.size or args.reset:
                    raise ValueError('Dokumenttyp angeben.')
                else:
                    result = {'standard':{'zeichen':DEFAULT_CHUNKING[0],'ueberlappung':DEFAULT_CHUNKING[1]},
                              'typen':{name:{'zeichen':size,'ueberlappung':overlap}
                                       for name,(size,overlap) in store.chunk_strategies().items()}}
            elif args.command == 'edit':
                result = {'aenderungen':store.update_metadata(args.id,args.title,args.category,
                                                               args.add_tags,args.remove_tags,args.status,
                                                               args.owner,args.review_date)}
            elif args.command == 'review-due':
                result = store.due_reviews(args.until)
            elif args.command == 'ai-retired':
                if args.mode:
                    store.set_exclude_retired(args.mode == 'exclude')
                result = {'ki_antworten_ohne_veraltete_und_archivierte':store.exclude_retired()}
            elif args.command == 'history':
                result = store.metadata_history(args.id)
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
                from retrieval import Retriever, snippet
                retrieval = Retriever(store).search(args.query,tag=args.tag,mode=args.mode)
                print(retrieval.status,file=sys.stderr)
                diagnosis = ('fulltext_rank','vector_rank','fulltext_score','vector_score','rrf_score')
                result = [{**{key:value for key,value in row.items() if args.diagnose or key not in diagnosis},
                           'fundstelle':snippet(row['content'],args.query)}
                          for row in retrieval.results]
                if args.diagnose:
                    result = {'modus':retrieval.mode,'status':retrieval.status,'vektoren':list(retrieval.vectors),
                              'laufzeit_ms':{name:round(1000*value,2) for name,value in retrieval.timings.items()},
                              'treffer':result}
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
                if args.command=='ask':  # same evidence rules as the desktop (step 7.4)
                    answered = ai.ask(args.query)
                    result = {'quellenlage':answered.state,'status':answered.status,
                              'answer':answered.text,'sources':answered.sources}
                    exit_code = ASK_EXIT_CODES[answered.state]
                    if answered.state != 'sufficient':
                        print(answered.label,file=sys.stderr)
                else:
                    answer,sources = ai.summarize(args.id)
                    result = {'answer':answer,'sources':sources}
            print(json.dumps(result,ensure_ascii=False,indent=2))
            if args.command=='ask':
                metadata={**metadata,'status':result['quellenlage']}  # the state only, never question or answer
            _log_event(logger,'command_completed',metadata=metadata)
            return exit_code
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

# ask: an answer (also a weak one) exits with 0, a refusal with 2, unavailable retrieval with 1.
ASK_EXIT_CODES = {'sufficient': 0, 'weak': 0, 'insufficient': 2, 'unavailable': 1}


def vector_command(store, args):
    from vector_sync import VectorSync
    sync = VectorSync(store)
    if args.activate:
        sync.activate(args.model)
    elif args.deactivate:
        sync.deactivate()
    elif args.run:
        state = sync.run()
        embedded = state['embedded']
        while not state['failed'] and state['embedded'] and sync.store.pending_vectors(state['model'], limit=1):
            state = sync.run()
            embedded += state['embedded']
        if state['failed']:
            raise ValueError(state['last_result'])
    state = sync.config()
    done, total = sync.progress()
    result = {'aktiv': state['enabled'], 'modell': state['model'], 'eingebettet': done, 'abschnitte': total,
              'letztes_ergebnis': state['last_result']}
    if args.run:
        result['in_diesem_lauf'] = embedded
    return result


if __name__ == '__main__':
    raise SystemExit(main())
