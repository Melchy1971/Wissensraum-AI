"""Native desktop interface. Run: python app.py"""
from pathlib import Path
import os
import json
import queue
import sys
import threading
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from tkinter.scrolledtext import ScrolledText
except (ImportError, OSError):
    tk=ttk=filedialog=messagebox=simpledialog=ScrolledText=None
from import_jobs import MAIL_KINDS, OUTCOME_LABELS, REASON_LABELS, ImportJobs
import mail_cleanup
import rules
from rules_view import RulesPage
from sync_view import SyncPanel
from vector_view import VectorPanel
from retrieval import Retriever, snippet
from settings import Settings, SystemVault, PROVIDERS
from database import DatabaseSettings, resolve_sqlite_path
from diagnostics import DiagnosticResult, format_diagnostics, health_overview, run_diagnostics
from design import DesignSettings, THEMES
from configuration import (APPLY_ERROR, EXPORT_ERROR, ConfigurationTransfer,
                           ConfigurationTransferError, recover_pending_configuration)
from importer import Importer, MailAccounts, MAIL_PROVIDERS
from migrations import migrate_sqlite, restore_sqlite
from local_logging import (create_operational_logger, known_runtime_secrets,
                           safe_exception_class, warn_operational_logging_failure)

BASE = Path(__file__).resolve().parent


class StartupFailure(RuntimeError):
    def __init__(self,results):
        self.results=tuple(results)
        messages=' '.join(result.message for result in self.results if result.status=='FAIL')
        super().__init__('Startdiagnose fehlgeschlagen. '+messages)


def prepare_startup(base=None,environ=None,vault_factory=None,
                    database_settings_factory=None,diagnostics_runner=None):
    """Run required checks and return unopened startup dependencies."""
    base=BASE if base is None else Path(base)
    environ=os.environ if environ is None else environ
    vault_factory=vault_factory or SystemVault
    database_settings_factory=database_settings_factory or DatabaseSettings
    diagnostics_runner=diagnostics_runner or run_diagnostics
    vault=vault_factory()
    database_settings=database_settings_factory(base,vault,environ)
    sqlite_path=resolve_sqlite_path(base,environ)
    results=tuple(diagnostics_runner(base,vault,database_settings,sqlite_path))
    if any(result.status=='FAIL' for result in results):
        raise StartupFailure(results)
    try:
        recover_pending_configuration(base,database_settings,sqlite_path)
    except ConfigurationTransferError:
        failure=DiagnosticResult('configuration_recovery','FAIL',
                                 'Konfigurationswiederherstellung ist unvollständig; das Wiederherstellungsjournal wurde beibehalten.',{})
        raise StartupFailure((failure,)) from None
    return vault,database_settings,results,sqlite_path


def initialize_desktop(window,database_settings,sqlite_path,root_initializer):
    """Create the actual application root before opening the active store."""
    try:
        root_initializer()
    except Exception:
        failure=DiagnosticResult('tcl_tk','FAIL',
            'Tcl/Tk oder die grafische Anzeige ist nicht verfügbar; Python mit Tcl/Tk installieren oder die CLI verwenden.',{})
        raise StartupFailure((failure,)) from None
    try:
        return database_settings.open_store(sqlite_path)
    except Exception:
        try:
            window.destroy()
        except Exception:
            pass
        failure=DiagnosticResult('database','FAIL',
                                 'Aktive Datenbank konnte nicht geöffnet werden; Konfiguration, Schema und Rechte prüfen.',
                                 {'backend':'Unknown'})
        raise StartupFailure((failure,)) from None


def run_with_root_cleanup(window,operation):
    """Destroy a possibly-created root when startup cannot complete."""
    try:
        return operation()
    except Exception:
        try:
            window.destroy()
        except Exception:
            pass
        raise


def destroy_root_on_startup_failure(initializer):
    def guarded(window,*args,**kwargs):
        return run_with_root_cleanup(window,lambda:initializer(window,*args,**kwargs))
    return guarded


def show_startup_failure(results):
    text='Startdiagnose fehlgeschlagen:\n'+'\n'.join(
        '- '+result.message for result in results if result.status=='FAIL')
    if tk is not None and messagebox is not None:
        root=None
        try:
            root=tk.Tk()
            root.withdraw()
            messagebox.showerror('Wissensraum – Startdiagnose',text,parent=root)
            return
        except Exception:
            pass
        finally:
            if root is not None:
                try: root.destroy()
                except Exception: pass
    print(text,file=sys.stderr)


ALL_TAGS = 'alle Tags'
LIST_COLUMNS = ('title','category','tags')
SEARCH_COLUMNS = ('title','section','hit','score')


def score_text(hit):
    """RRF score with the ranks in both lists; no reranking score, there is no reranker (D-019)."""
    ranks = ', '.join(f'{name} {hit[key]}' for name,key in (('Volltext','fulltext_rank'),('Vektor','vector_rank'))
                      if hit.get(key))
    return f'{hit["rrf_score"]:.4f} ({ranks})' if 'rrf_score' in hit else ranks


class App(tk.Tk if tk is not None else object):
    @destroy_root_on_startup_failure
    def __init__(self,startup=None):
        if tk is None:
            raise StartupFailure((DiagnosticResult('tcl_tk','FAIL',
                'Tcl/Tk konnte nicht geladen werden; Python mit Tcl/Tk installieren oder die CLI verwenden.',{}),))
        if startup is None:
            vault,database_settings,diagnostic_results,sqlite_path=prepare_startup()
            store=initialize_desktop(self,database_settings,sqlite_path,lambda:tk.Tk.__init__(self))
        else:
            # Preserve the injected startup tuple used by GUI tests and embedders.
            store,vault,database_settings,diagnostic_results=startup
            try:
                tk.Tk.__init__(self)
            except Exception:
                failure=DiagnosticResult('tcl_tk','FAIL',
                    'Tcl/Tk oder die grafische Anzeige ist nicht verfügbar; Python mit Tcl/Tk installieren oder die CLI verwenden.',{})
                raise StartupFailure((*diagnostic_results,failure)) from None
        self.title('Wissensraum · SQL & KI')
        self.geometry('1180x780')
        self.minsize(920, 650)
        self.configure(bg='#f3f4f0')
        self.store=store
        self.retriever=Retriever(store)  # one per window: keeps the small cache of question vectors
        self._operational_logger=None
        self._operational_logging_closed=False
        self.vault=vault
        self.database_settings=database_settings
        self.diagnostic_results=diagnostic_results
        problems=[result.message for result in self.diagnostic_results if result.status=='WARN']
        notice='Startdiagnose: '+' '.join(problems) if problems else ''
        self.diagnostic_notice=tk.StringVar(value=notice)
        self.queue = queue.Queue()
        self.busy = False
        self.settings = Settings(self.store, BASE, self.vault)
        self.design_settings = DesignSettings(BASE)
        self.configuration_transfer = ConfigurationTransfer(
            self.store, BASE, self.database_settings, self.design_settings)
        self.configuration_transfer.synchronize_authority()
        self.theme_mode = self.design_settings.theme()
        self.mail_accounts = MailAccounts(self.store, self.settings.vault, BASE)
        self.importer = Importer(self.store, self.mail_accounts)
        self.import_jobs = ImportJobs(self.store, self.importer)
        self.import_cancel = None
        self.import_progress = ''
        self.docs = {}
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.apply_theme(self.theme_mode)
        self.brand_bar=tk.Frame(self,height=6,bg=THEMES[self.theme_mode]['accent'])
        self.brand_bar.pack(fill='x')
        header = ttk.Frame(self, padding=(24,14))
        header.pack(fill='x')
        self.logo_label=ttk.Label(header)
        self.logo_label.pack(side='left',padx=(0,14))
        ttk.Label(header,text='Wissensraum',style='BrandTitle.TLabel').pack(side='left')
        ttk.Label(header,text='Dokumente verstehen. Wissen wiederfinden.',style='Muted.TLabel').pack(side='left',padx=24)
        self.logo_image=None
        self.refresh_logo()
        tools = ttk.Frame(self, padding=(24,0,24,12))
        tools.pack(fill='x')
        self.buttons = []
        for label, command in [('Import',self.open_import),('Notiz erfassen',self.note),
                               ('Datenbank sichern',self.backup),('Einstellungen',self.open_settings)]:
            b = ttk.Button(tools,text=label,command=command)
            b.pack(side='left',padx=(0,8))
            self.buttons.append(b)
        self.exit_button = ttk.Button(tools,text='Beenden',command=self.exit_application,style='Exit.TButton')
        self.exit_button.pack(side='right')
        if notice:
            ttk.Label(self,textvariable=self.diagnostic_notice,style='Warning.TLabel',wraplength=1100,
                      padding=(24,0,24,12)).pack(fill='x')
        self.protocol('WM_DELETE_WINDOW',self.exit_application)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill='both',expand=True,padx=24,pady=(0,12))
        self.library = ttk.Frame(self.tabs,padding=16)
        self.chat = ttk.Frame(self.tabs,padding=16)
        self.history_tab = ttk.Frame(self.tabs,padding=16)
        self.tabs.add(self.library,text='Dokumente & Suche')
        self.tabs.add(self.chat,text='Wissen befragen')
        self.tabs.add(self.history_tab,text='KI-Verlauf')
        self.import_tab = ttk.Frame(self.tabs,padding=16)
        self.tabs.add(self.import_tab,text='Import')
        self.settings_tab = ttk.Frame(self.tabs,padding=16)
        self.tabs.add(self.settings_tab,text='Einstellungen')
        search = ttk.Frame(self.library)
        search.pack(fill='x',pady=(0,12))
        self.query = tk.StringVar()
        entry = ttk.Entry(search,textvariable=self.query)
        entry.pack(side='left',fill='x',expand=True)
        entry.bind('<Return>',lambda _:self.refresh())
        ttk.Button(search,text='Suchen',command=self.refresh).pack(side='left',padx=(8,0))
        ttk.Button(search,text='Alle anzeigen',command=self.clear_search).pack(side='left',padx=(8,0))
        ttk.Label(search,text='Tag').pack(side='left',padx=(16,4))
        self.tag_filter = tk.StringVar(value=ALL_TAGS)
        self.tag_box = ttk.Combobox(search,textvariable=self.tag_filter,state='readonly',width=22,values=[ALL_TAGS])
        self.tag_box.pack(side='left')
        self.tag_box.bind('<<ComboboxSelected>>',lambda _:self.refresh())
        split = ttk.Panedwindow(self.library,orient='horizontal')
        split.pack(fill='both',expand=True)
        left, right = ttk.Frame(split), ttk.Frame(split)
        split.add(left,weight=2)
        split.add(right,weight=3)
        self.tree = ttk.Treeview(left,columns=('title','category','tags','section','hit','score'),show='headings',
                                 selectmode='browse')
        for col,title,width in [('title','Titel',230),('category','Bereich',110),('tags','Tags',110),
                                ('section','Abschnitt',70),('hit','Fundstelle',320),('score','Score',150)]:
            self.tree.heading(col,text=title)
            self.tree.column(col,width=width)
        self.tree.configure(displaycolumns=LIST_COLUMNS)
        self.hits = {}  # document id -> search result of the last search (Fundstelle, ranks, score)
        scroll = ttk.Scrollbar(left,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.show_document())
        row = ttk.Frame(right)
        row.pack(fill='x',padx=(14,0),pady=(0,8))
        for label,command in [('Zusammenfassen',self.summarize),('Metadaten',self.edit_metadata),
                              ('Versionen',self.show_versions),
                              ('Original exportieren',self.export),('Löschen',self.delete)]:
            b = ttk.Button(row,text=label,command=command)
            b.pack(side='left',padx=(0,5))
            self.buttons.append(b)
        self.detail = ScrolledText(right,wrap='word',font=('Segoe UI',11),padx=14,pady=12,
                                   background='white',foreground='#182c2a')
        self.detail.pack(fill='both',expand=True,padx=(14,0))
        self.write(self.detail,'Noch keine Auswahl. Importiere Dokumente oder erfasse eine Notiz.\n\nDateiformate: TXT, Markdown, PDF mit Text und DOCX.')
        ttk.Label(self.chat,text='Frage an deine Dokumente',font=('Segoe UI',16,'bold')).pack(anchor='w')
        self.provider_notice = tk.StringVar()
        ttk.Label(self.chat,textvariable=self.provider_notice,wraplength=820).pack(anchor='w',pady=(4,10))
        question_row = ttk.Frame(self.chat)
        question_row.pack(fill='x')
        self.question = tk.StringVar()
        field = ttk.Entry(question_row,textvariable=self.question)
        field.pack(side='left',fill='x',expand=True)
        field.bind('<Return>',lambda _:self.ask())
        b = ttk.Button(question_row,text='Quellen suchen & antworten',command=self.ask,style='Accent.TButton')
        b.pack(side='left',padx=(8,0))
        self.buttons.append(b)
        self.evidence_notice = tk.StringVar()
        self.evidence_label = ttk.Label(self.chat,textvariable=self.evidence_notice,font=('Segoe UI',12,'bold'),wraplength=820)
        self.evidence_label.pack(anchor='w',pady=(12,0))
        self.answer = ScrolledText(self.chat,wrap='word',font=('Segoe UI',12),padx=18,pady=16,
                                   background='white',foreground='#182c2a')
        self.answer.pack(fill='both',expand=True,pady=(14,0))
        self.write(self.answer,'Stelle eine Frage zu deinen Dokumenten.\n\nVor jeder KI-Anfrage prüft die Anwendung die Quellenlage. Ohne belastbare Quelle wird keine Antwort erzeugt. Antworten zeigen Quellen als [Q1], [Q2] usw. Prüfe die zugrunde liegenden Textstellen. KI-Aussagen können fehlerhaft sein.')
        ttk.Button(self.history_tab,text='Verlauf aktualisieren',command=self.show_history).pack(anchor='w',pady=(0,12))
        self.history = ScrolledText(self.history_tab,wrap='word',font=('Segoe UI',11),padx=14,pady=12)
        self.history.pack(fill='both',expand=True)
        source='PostgreSQL' if self.store.backend=='postgresql' else 'SQLite · Einzelplatz'
        self.status = tk.StringVar(value='Bereit · '+source)
        ttk.Label(self,textvariable=self.status,padding=(24,0,24,12)).pack(fill='x')
        self.build_import()
        self.build_settings()
        self.apply_theme(self.theme_mode)
        self.refresh()
        self.show_history()
        self.after(100,self.poll)

    def apply_theme(self,theme):
        """Apply the selected Telekom-oriented palette to existing and future widgets."""
        palette=THEMES[theme]
        self.theme_mode=theme
        self.palette=palette
        self.configure(bg=palette['background'])
        for canvas in getattr(self,'settings_canvases',()):
            canvas.configure(bg=palette['background'])
        style=self.style
        style.configure('.',font=('Segoe UI',11),background=palette['background'],foreground=palette['text'])
        style.configure('TFrame',background=palette['background'])
        style.configure('TLabel',background=palette['background'],foreground=palette['text'])
        style.configure('Muted.TLabel',background=palette['background'],foreground=palette['muted'])
        style.configure('BrandTitle.TLabel',background=palette['background'],foreground=palette['accent'],font=('Segoe UI',24,'bold'))
        style.configure('Warning.TLabel',background=palette['background'],foreground=palette['danger'])
        style.configure('TLabelframe',background=palette['background'],foreground=palette['text'],bordercolor=palette['border'])
        style.configure('TLabelframe.Label',background=palette['background'],foreground=palette['text'])
        style.configure('TButton',padding=(12,8),background=palette['surface'],foreground=palette['text'],bordercolor=palette['border'])
        style.map('TButton',background=[('active',palette['input']),('disabled',palette['background'])],
                  foreground=[('disabled',palette['muted'])])
        style.configure('Accent.TButton',background=palette['accent'],foreground='white',bordercolor=palette['accent'])
        style.map('Accent.TButton',background=[('active',palette['accent_active']),('disabled',palette['disabled'])],
                  foreground=[('disabled','#eeeeee')])
        style.configure('Exit.TButton',background=palette['danger'],foreground='white',bordercolor=palette['danger'])
        style.map('Exit.TButton',background=[('active',palette['danger_active'])])
        style.configure('TEntry',fieldbackground=palette['input'],foreground=palette['text'],insertcolor=palette['text'])
        style.configure('TCombobox',fieldbackground=palette['input'],background=palette['input'],foreground=palette['text'],arrowcolor=palette['text'])
        style.map('TCombobox',fieldbackground=[('readonly',palette['input'])],foreground=[('readonly',palette['text'])])
        style.configure('TSpinbox',fieldbackground=palette['input'],background=palette['input'],foreground=palette['text'],arrowcolor=palette['text'])
        style.configure('TRadiobutton',background=palette['background'],foreground=palette['text'])
        style.map('TRadiobutton',background=[('active',palette['background'])],foreground=[('active',palette['text'])])
        style.configure('Treeview',rowheight=32,fieldbackground=palette['surface'],background=palette['surface'],foreground=palette['text'])
        style.configure('Treeview.Heading',background=palette['background'],foreground=palette['text'],bordercolor=palette['border'])
        style.map('Treeview',background=[('selected',palette['accent'])],foreground=[('selected','white')])
        style.configure('TNotebook',background=palette['background'],bordercolor=palette['border'])
        style.configure('TNotebook.Tab',padding=(10,6),background=palette['background'],foreground=palette['text'])
        style.map('TNotebook.Tab',background=[('selected',palette['accent']),('active',palette['surface'])],
                  foreground=[('selected','white'),('active',palette['text'])])
        if hasattr(self,'brand_bar'):
            self.brand_bar.configure(bg=palette['accent'])
        for name in ('detail','answer','history'):
            widget=getattr(self,name,None)
            if widget:
                self.configure_text_widget(widget)
        if hasattr(self,'provider_tree'):
            self.provider_tree.tag_configure('active',foreground=palette['accent'],font=('Segoe UI',11,'bold'))

    def configure_text_widget(self,widget):
        widget.configure(background=self.palette['surface'],foreground=self.palette['text'],
                         insertbackground=self.palette['text'],selectbackground=self.palette['accent'],
                         selectforeground='white')

    def refresh_logo(self):
        self.logo_image=None
        self.logo_load_error=''
        if self.design_settings.logo_path.is_file():
            try:
                from PIL import Image, ImageTk
            except Exception:
                self.logo_load_error='Logo konnte wegen einer fehlerhaften Bildbibliothek nicht geladen werden.'
            else:
                try:
                    with Image.open(self.design_settings.logo_path) as source:
                        image=source.convert('RGBA')
                        image.thumbnail((180,60),Image.Resampling.LANCZOS)
                        self.logo_image=ImageTk.PhotoImage(image)
                except Exception:
                    self.logo_image=None
                    self.logo_load_error='Gespeichertes Logo ist nicht lesbar; unter Einstellungen → Design neu auswählen.'
        if hasattr(self,'logo_label'):
            self.logo_label.configure(image=self.logo_image or '')
        preview=getattr(self,'design_logo_preview',None)
        if preview:
            preview.configure(image=self.logo_image or '',text='' if self.logo_image else 'Kein Logo hinterlegt')

    def exit_application(self):
        """End the desktop application immediately from every close control."""
        self.stop_operational_logging()
        self.destroy()

    def stop_operational_logging(self):
        if not self._operational_logging_closed:
            self._safe_operational_event('application_stopped',metadata={'backend':self.store.backend})
        self.close_operational_logger()

    def attach_operational_logger(self,logger):
        """Attach an optional logger after desktop startup has fully succeeded."""
        self._operational_logger=logger
        self._operational_logging_closed=False
        self._safe_operational_event('application_started',metadata={'backend':self.store.backend})

    def _safe_operational_event(self,event_name,**kwargs):
        logger=getattr(self,'_operational_logger',None)
        if logger is None or getattr(self,'_operational_logging_closed',False):
            return
        try:
            logger.event(event_name,**kwargs)
        except BaseException:
            warn_operational_logging_failure()

    def close_operational_logger(self):
        if getattr(self,'_operational_logging_closed',False):
            return
        self._operational_logging_closed=True
        logger=getattr(self,'_operational_logger',None)
        if logger is not None:
            try:
                logger.close()
            except BaseException:
                warn_operational_logging_failure()

    def write(self, widget, text):
        widget.configure(state='normal')
        widget.delete('1.0','end')
        widget.insert('1.0',text)
        widget.configure(state='disabled')

    def selected(self):
        selected = self.tree.selection()
        if not selected:
            raise ValueError('Zuerst ein Dokument auswählen.')
        return int(selected[0])

    def clear_search(self):
        self.query.set('')
        self.refresh()

    def refresh(self):
        try:
            query = self.query.get().strip()
            tag = self.tag_filter.get()
            tag = None if tag == ALL_TAGS else tag.rsplit(' (',1)[0]
            if query:
                # The question vector comes from Ollama and the first search loads the vector matrix:
                # both run in the worker thread so the window stays responsive.
                self.work(lambda:self.retriever.search(query,limit=30,tag=tag),
                          lambda retrieval:self.show_rows([self.store.get(r['document_id']) for r in retrieval.results],
                                                          f' · {retrieval.status}'+(f' · Tag {tag}' if tag else ''),
                                                          {r['document_id']:dict(r,fundstelle=snippet(r['content'],query))
                                                           for r in retrieval.results}))
                return
            self.show_rows(self.store.list(tag=tag),f' · Tag {tag}' if tag else '')
        except Exception as e:
            messagebox.showerror('Suche fehlgeschlagen',str(e),parent=self)

    def show_rows(self,rows,suffix,hits=None):
        """Documents of the library; with ``hits`` (search) also section, Fundstelle and score of each hit."""
        with self.store.connect() as db:
            tags = rules.tags_by_document(db)
            counts = rules.tag_counts(db)
        self.tag_box.configure(values=[ALL_TAGS]+[f'{name} ({count})' for name,count in counts])
        self.hits = hits or {}
        self.tree.configure(displaycolumns=SEARCH_COLUMNS if hits is not None else LIST_COLUMNS)
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            hit = self.hits.get(r['id'])
            found = (hit['ordinal'],hit['fundstelle'],score_text(hit)) if hit else ('','','')
            self.tree.insert('', 'end', iid=str(r['id']),values=(r['title'],r['category'],
                                                                 ', '.join(tags.get(r['id'],[])),*found))
        self.status.set(f'{len(rows)} Dokumente angezeigt{suffix}')

    def show_document(self):
        if self.tree.selection():
            doc = self.store.get(self.selected())
            versions = self.store.versions(doc['id'])
            version = next((row['version'] for row in versions if row['id']==doc['id']),1)
            line = f'Version {version} von {len(versions)}' if len(versions)>1 else 'Version 1'
            with self.store.connect() as db:
                tags = ', '.join(rules.document_tags(db,doc['id'])) or 'keine'
            hit = self.hits.get(doc['id'])
            found = (f'Fundstelle: Abschnitt {hit["ordinal"]} · {score_text(hit)}\n{hit["fundstelle"]}\n'
                     if hit else '')
            self.write(self.detail,f'{doc["title"]}\nBereich: {doc["category"]}\nTags: {tags}\n'
                                   f'Erfasst: {doc["created_at"]} UTC\n{line}\n{found}\n{doc["content"]}')

    def edit_metadata(self):
        """Title, area and tags of the selected document with its change history (step 8A.1)."""
        doc_id=self.selected()
        doc=self.store.get(doc_id)
        with self.store.connect() as db:
            tags=rules.document_tags(db,doc_id)
        window=tk.Toplevel(self)
        window.title('Metadaten')
        window.geometry('760x560')
        window.transient(self)
        ttk.Label(window,text='Metadaten',font=('Segoe UI',18,'bold'),padding=(18,16,18,0)).pack(anchor='w')
        ttk.Label(window,text='Inhalt und Originaldatei bleiben unverändert. Jede Änderung wird im Verlauf protokolliert.',
                  padding=(18,5,18,10)).pack(anchor='w')
        form=ttk.Frame(window,padding=(18,0,18,8)); form.pack(fill='x')
        fields={}
        for row,(name,label,value) in enumerate([('title','Titel',doc['title']),('category','Bereich',doc['category']),
                                                 ('tags','Tags (kommagetrennt)',', '.join(tags))]):
            ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',pady=3)
            fields[name]=tk.StringVar(value=value)
            ttk.Entry(form,textvariable=fields[name],width=70).grid(row=row,column=1,sticky='we',pady=3,padx=(10,0))
        form.columnconfigure(1,weight=1)
        tree=ttk.Treeview(window,columns=('created','field','old','new','origin'),show='headings',height=10)
        for col,title,width in [('created','Zeitpunkt (UTC)',140),('field','Feld',80),('old','Vorher',170),
                                ('new','Nachher',170),('origin','Herkunft',90)]:
            tree.heading(col,text=title); tree.column(col,width=width)
        labels={'title':'Titel','category':'Bereich','tag':'Tag'}
        origins={'manual':'manuell','rule':'Regel','rule_revert':'Regel zurück'}
        def show_history():
            tree.delete(*tree.get_children())
            for number,row in enumerate(reversed(self.store.metadata_history(doc_id))):
                tree.insert('','end',iid=str(number),values=(row['created_at'],labels.get(row['field'],row['field']),
                                                             row['old_value'],row['new_value'],
                                                             origins.get(row['origin'],row['origin'])))
        def save():
            try:
                wanted=rules.parse_tags(fields['tags'].get())
                with self.store.connect() as db:
                    current=rules.document_tags(db,doc_id)
                keep={name.casefold() for name in wanted}
                self.store.update_metadata(doc_id,fields['title'].get(),fields['category'].get(),wanted,
                                           [name for name in current if name.casefold() not in keep])
            except ValueError as error:
                messagebox.showerror('Metadaten',str(error),parent=window)
                return
            show_history()
            self.refresh()
            if self.tree.exists(str(doc_id)):
                self.tree.selection_set(str(doc_id))
        buttons=ttk.Frame(window,padding=(18,0,18,8)); buttons.pack(fill='x')
        ttk.Button(buttons,text='Speichern',command=save,style='Accent.TButton').pack(side='left')
        ttk.Label(window,text='Verlauf (neueste zuerst)',padding=(18,4,18,4)).pack(anchor='w')
        tree.pack(fill='both',expand=True,padx=18,pady=(0,16))
        show_history()
        window.metadata_fields=fields; window.metadata_save=save; window.metadata_tree=tree
        return window

    def show_versions(self):
        doc_id=self.selected()
        versions=self.store.versions(doc_id)
        window=tk.Toplevel(self)
        window.title('Versionen')
        window.geometry('900x560')
        window.transient(self)
        ttk.Label(window,text='Versionen',font=('Segoe UI',18,'bold'),padding=(18,16,18,0)).pack(anchor='w')
        ttk.Label(window,text='Neueste zuerst. Suche und Fragen verwenden nur die aktuelle Version.',
                  padding=(18,5,18,10)).pack(anchor='w')
        body=ttk.Panedwindow(window,orient='horizontal'); body.pack(fill='both',expand=True,padx=18,pady=(0,16))
        tree=ttk.Treeview(body,columns=('version','created','title'),show='headings',selectmode='browse')
        for col,title,width in [('version','Version',70),('created','Erfasst (UTC)',150),('title','Titel',200)]:
            tree.heading(col,text=title); tree.column(col,width=width)
        text=ScrolledText(body,wrap='word',font=('Segoe UI',11),padx=12,pady=10)
        body.add(tree,weight=2); body.add(text,weight=3)
        for row in versions:
            tree.insert('','end',iid=str(row['id']),values=(row['version'],row['created_at'],row['title']))
        def show(_=None):
            if tree.selection():
                self.write(text,self.store.get(int(tree.selection()[0]))['content'])
        tree.bind('<<TreeviewSelect>>',show)
        window.version_tree=tree; window.version_text=text
        if versions:
            tree.selection_set(str(doc_id) if tree.exists(str(doc_id)) else tree.get_children()[0])
            show()
        return window

    def work(self, fn, done):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.configure(state='disabled')
        self.status.set('Wird verarbeitet …')
        self._safe_operational_event('background_operation_started',metadata={'operation':'background'})
        def worker():
            try:
                result=fn()
                self._safe_operational_event('background_operation_completed',metadata={'operation':'background'})
                self.queue.put((done,result,None))
            except Exception as e:
                self._safe_operational_event('background_operation_failed',severity='ERROR',
                    metadata={'operation':'background'},exception_class=safe_exception_class(e),
                    error_code='DESKTOP_BACKGROUND_FAILED')
                self.queue.put((done,None,str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        # Worker threads only set plain attributes; Tk widgets are updated here.
        if self.import_progress and self.import_message.get()!=self.import_progress:
            self.import_message.set(self.import_progress)
        try:
            done,result,error = self.queue.get_nowait()
            self.busy = False
            for button in self.buttons:
                button.configure(state='normal')
            if error:
                if self.import_cancel is not None:
                    self.end_import_job()
                self.status.set('Aktion fehlgeschlagen')
                messagebox.showerror('Aktion fehlgeschlagen',error,parent=self)
            else:
                self.status.set('Erledigt')
                done(result)
        except queue.Empty:
            pass
        self.after(100,self.poll)

    def import_files(self):
        paths = filedialog.askopenfilenames(filetypes=[('Dokumente','*.txt *.md *.pdf *.docx *.html *.htm *.rtf *.xlsx *.pptx *.eml'),
                                                       ('Bilder (mit OCR)','*.png *.jpg *.jpeg *.tif *.tiff'),
                                                       ('Alle Dateien','*.*')])
        if not paths:
            return
        self.preview_import(lambda cancel,progress:self.import_jobs.preview_files(paths,cancel,progress),
                            lambda rows,everything:self.import_jobs.create_for_files([row['path'] for row in rows]))

    def import_local_folder(self):
        folder=filedialog.askdirectory(title='Ordner mit Dokumenten auswählen')
        if not folder:
            return
        # All rows: the folder job also reports unreadable subfolders. A subset: only the chosen files.
        self.preview_import(lambda cancel,progress:self.import_jobs.preview_folder(folder,cancel,progress),
                            lambda rows,everything:self.import_jobs.create_for_folder(folder) if everything else
                            self.import_jobs.create_selection('folder',folder,[(row['path'],row['locator']) for row in rows]))

    def preview_import(self,analyse,create_job):
        """Read the files in the background and show what an import would do; nothing is stored yet."""
        cancel=threading.Event()
        self.import_cancel=cancel
        self.import_cancel_button.configure(state='normal')
        def progress(done,total,locator):
            self.import_progress=f'Vorschau: {done} von {total} · {locator}'
        def finish(rows):
            self.end_import_job()
            if rows is None:
                self.import_message.set('Vorschau abgebrochen. Es wurde nichts gespeichert.')
                return
            self.show_import_preview(rows,create_job)
        self.work(lambda:analyse(cancel,progress),finish)

    @staticmethod
    def preview_outcome(row):
        if row['outcome']=='version':
            return f'neue Version {row["version"]} von „{row["existing_title"]}“'
        if row['outcome']=='quarantined':
            return 'Quarantäne: '+REASON_LABELS.get(row['reason'],row['reason'])
        if row['outcome']=='ocr':
            pages=row.get('pages') or 1
            return f'wird beim Import per OCR gelesen ({pages} {"Seite" if pages==1 else "Seiten"})'
        return OUTCOME_LABELS[row['outcome']]

    @staticmethod
    def file_size_text(size):
        if size is None:
            return ''
        return f'{size} B' if size<1024 else (f'{size/1024:.1f} KB' if size<1024*1024 else f'{size/1024/1024:.1f} MB')

    def show_import_preview(self,rows,create_job):
        window=tk.Toplevel(self)
        window.title('Importvorschau')
        window.geometry('1080x650')
        window.minsize(820,480)
        window.transient(self)
        ttk.Label(window,text='Importvorschau',font=('Segoe UI',18,'bold'),padding=(18,16,18,0)).pack(anchor='w')
        counts={key:sum(row['outcome']==key for row in rows) for key in OUTCOME_LABELS}
        window.preview_summary=(f'{len(rows)} Dateien · {counts["new"]} neu · {counts["version"]} neue Version · '
                                f'{counts["duplicate"]} bereits vorhanden · {counts["ocr"]} per OCR beim Import · '
                                f'{counts["quarantined"]} Quarantäne · Noch ist nichts gespeichert.')
        ttk.Label(window,text=window.preview_summary,padding=(18,5,18,10),wraplength=1000).pack(anchor='w')
        # Buttons first and at the bottom: they stay visible however small the window or large the font.
        actions=ttk.Frame(window,padding=(18,8,18,16)); actions.pack(side='bottom',fill='x')
        controls=ttk.Frame(window,padding=(18,8,18,0)); controls.pack(side='bottom',fill='x')
        body=ttk.Panedwindow(window,orient='vertical'); body.pack(fill='both',expand=True,padx=18)
        columns=('active','file','type','size','title','outcome')
        tree=ttk.Treeview(body,columns=columns,show='headings',selectmode='extended',height=8)
        for col,title,width in [('active','Auswahl',70),('file','Datei',250),('type','Typ',55),('size','Größe',80),
                                ('title','Titel',210),('outcome','Ergebnis',290)]:
            tree.heading(col,text=title); tree.column(col,width=width,stretch=col in ('file','title','outcome'))
        excerpt=ScrolledText(body,wrap='word',font=('Segoe UI',11),padx=12,pady=10,height=5)
        body.add(tree,weight=3); body.add(excerpt,weight=1)
        window.selected=[True]*len(rows)
        for index,row in enumerate(rows):
            tree.insert('','end',iid=str(index),values=('JA',row['locator'],row['type'],self.file_size_text(row['size']),
                                                       row['title'],self.preview_outcome(row)))
        window.preview_status=tk.StringVar()
        def refresh_status():
            chosen=sum(window.selected)
            window.preview_status.set(f'{chosen} von {len(rows)} Dateien zum Import ausgewählt. '
                                      'Doppelklick oder „Markierte umschalten“ ändert die Auswahl.')
            window.import_button.configure(state='normal' if chosen else 'disabled')
        def set_rows(indexes,value=None):
            for index in indexes:
                window.selected[index]=(not window.selected[index]) if value is None else value
                values=list(tree.item(str(index),'values')); values[0]='JA' if window.selected[index] else 'NEIN'
                tree.item(str(index),values=values,tags=() if window.selected[index] else ('off',))
            refresh_status()
        tree.tag_configure('off',foreground='#8a8f98')
        window.set_rows=set_rows
        for title,command in (('Alle auswählen',lambda:set_rows(range(len(rows)),True)),
                              ('Keine auswählen',lambda:set_rows(range(len(rows)),False)),
                              ('Markierte umschalten',lambda:set_rows([int(iid) for iid in tree.selection()]))):
            ttk.Button(controls,text=title,command=command).pack(side='left',padx=(0,8))
        ttk.Label(controls,textvariable=window.preview_status).pack(side='left',padx=(8,0))
        def toggle_at(event):
            iid=tree.identify_row(event.y)
            if iid: set_rows([int(iid)])
        tree.bind('<Double-1>',toggle_at)
        def show(_=None):
            if tree.selection():
                row=rows[int(tree.selection()[0])]
                self.write(excerpt,row['excerpt'] or ('Scan oder Bild: Der Text wird beim Import per OCR erkannt.'
                                                      if row['outcome']=='ocr' else 'Kein lesbarer Text.'))
        tree.bind('<<TreeviewSelect>>',show)
        window.preview_tree=tree; window.preview_excerpt=excerpt
        def start():
            chosen=[row for row,active in zip(rows,window.selected) if active]
            if not chosen:
                return
            everything=len(chosen)==len(rows)
            close()
            self.run_import_job(lambda:create_job(chosen,everything))
        def close():
            if window.import_button in self.buttons: self.buttons.remove(window.import_button)
            if window.winfo_exists(): window.destroy()
        window.import_button=ttk.Button(actions,text='Importieren',style='Accent.TButton',command=start)
        window.import_button.pack(side='right')
        ttk.Button(actions,text='Abbrechen',command=close).pack(side='right',padx=(0,8))
        self.buttons.append(window.import_button)
        window.protocol('WM_DELETE_WINDOW',close)
        window.start_import=start
        refresh_status()
        if rows:
            tree.selection_set('0'); show()
        self.import_message.set('Vorschau bereit: '+window.preview_summary)
        self.import_preview_window=window
        return window

    def resume_import_job(self):
        open_jobs=[job for job in self.import_jobs.jobs() if job['resumable']]
        if not open_jobs:
            messagebox.showinfo('Importauftrag','Es gibt keinen offenen Importauftrag.',parent=self)
            return
        job_id=open_jobs[0]['job_id']
        self.run_import_job(lambda:job_id)

    @staticmethod
    def ocr_status_text():
        import ocr
        state=ocr.status()
        if state['available']:
            return 'Bilder und Scans werden mit OCR gelesen: '+state['message']
        return ('Bilder und Scans ohne Text landen in Quarantäne. '+state['message'])

    def retry_no_text(self):
        """Process items quarantined as "kein lesbarer Text" again, e.g. after OCR was installed."""
        import ocr
        self.ocr_notice.set(self.ocr_status_text())
        groups=self.import_jobs.retry_candidates('no_text')
        count=sum(len(entries) for entries in groups.values())
        if not count:
            messagebox.showinfo('Quarantäne','Kein Eintrag wartet auf Texterkennung.',parent=self)
            return
        if not ocr.available():
            messagebox.showinfo('Quarantäne',f'{count} Einträge warten auf Texterkennung. '
                                'Zuerst die optionale OCR einrichten (install-ocr-support.bat).',parent=self)
            return
        job_ids=self.import_jobs.retry_quarantined('no_text')
        cancel=threading.Event()
        self.import_cancel=cancel
        self.import_cancel_button.configure(state='normal')
        def progress(done,total,locator):
            self.import_progress=f'Texterkennung: {done} von {total} · {locator}'
        def run():
            summaries=[]
            for job_id in job_ids:  # one job per source; each stays resumable on its own
                summaries.append(self.import_jobs.run(job_id,cancel,progress))
                if cancel.is_set():
                    break
            return self.combined_summary(summaries)
        self.work(run,self.finish_import_job)

    @staticmethod
    def combined_summary(summaries):
        if len(summaries)==1:
            return summaries[0]
        total={key:sum(summary[key] for summary in summaries) for key in
               ('total','pending','running','imported','versioned','duplicate','quarantined','failed')}
        kinds={summary['kind'] for summary in summaries}
        return {**total,'job_id':', '.join(str(summary['job_id']) for summary in summaries),
                'kind':kinds.pop() if len(kinds)==1 else 'files',
                'status':'cancelled' if any(summary['status']=='cancelled' for summary in summaries) else 'completed',
                'resumable':any(summary['resumable'] for summary in summaries)}

    def run_import_job(self,create_job):
        """Run an import job in the background; progress is mirrored by poll()."""
        cancel=threading.Event()
        self.import_cancel=cancel
        self.import_cancel_button.configure(state='normal')
        def progress(done,total,locator):
            self.import_progress=f'Import läuft: {done} von {total} · {locator}'
        def run():
            return self.import_jobs.run(create_job(),cancel,progress)
        self.work(run,self.finish_import_job)

    def cancel_import_job(self):
        if self.import_cancel is not None:
            self.import_cancel.set()
            self.import_progress='Import wird nach der aktuellen Datei angehalten …'

    def end_import_job(self):
        self.import_cancel=None
        self.import_progress=''
        self.import_cancel_button.configure(state='disabled')

    @staticmethod
    def local_report_text(summary):
        state={'completed':'abgeschlossen','cancelled':'abgebrochen'}.get(summary['status'],summary['status'])
        unit='Einträge' if summary.get('kind') in MAIL_KINDS else 'Dateien'
        text=(f'Auftrag {summary["job_id"]} {state}: {summary["total"]} {unit} · {summary["imported"]} importiert · '
              f'{summary["versioned"]} neue Version · {summary["duplicate"]} bereits vorhanden · {summary["quarantined"]} in Quarantäne · '
              f'{summary["failed"]} fehlgeschlagen')
        return text+(' · mit „Auftrag fortsetzen“ weiterführen' if summary['resumable'] else '')

    def finish_import_job(self,summary):
        self.end_import_job()
        self.refresh()
        window=getattr(self,'mail_preview_window',None)
        if window is not None and summary.get('kind') in MAIL_KINDS:
            self.close_mail_preview(window)
            self.mail_preview_window=None
        text=self.local_report_text(summary)
        source={'imap':'Postfach','graph':'Postfach','pst':'PST'}.get(summary.get('kind'),'Lokale Dokumente')
        self.import_message.set(source+': '+text)
        messagebox.showinfo('Importergebnis',text+'\n\nDetails je Eintrag: „Bericht speichern“.',parent=self)

    def save_import_report(self):
        jobs=self.import_jobs.jobs(1)
        if not jobs:
            messagebox.showinfo('Importbericht','Es gibt noch keinen Importauftrag.',parent=self)
            return
        job_id=jobs[0]['job_id']
        target=filedialog.asksaveasfilename(defaultextension='.csv',initialfile=f'importbericht-{job_id}.csv',
                                            filetypes=[('CSV-Datei','*.csv')])
        if target:
            self.work(lambda:self.import_jobs.write_report(job_id,target),
                      lambda path:self.import_message.set(f'Importbericht gespeichert: {path}'))

    def note(self):
        window = tk.Toplevel(self)
        window.title('Notiz erfassen')
        window.geometry('680x520')
        window.transient(self)
        ttk.Label(window,text='Titel',padding=(16,12,16,4)).pack(anchor='w')
        title = ttk.Entry(window)
        title.pack(fill='x',padx=16)
        ttk.Label(window,text='Bereich',padding=(16,12,16,4)).pack(anchor='w')
        category = ttk.Entry(window)
        category.insert(0,'Allgemein')
        category.pack(fill='x',padx=16)
        content = ScrolledText(window,wrap='word',font=('Segoe UI',11))
        self.configure_text_widget(content)
        content.pack(fill='both',expand=True,padx=16,pady=16)
        def save():
            try:
                _,created = self.store.add(title.get(),content.get('1.0','end'),category.get())
                self.refresh()
                window.destroy()
                self.status.set('Notiz gespeichert' if created else 'Identischer Inhalt bereits vorhanden')
            except Exception as e:
                messagebox.showerror('Speichern fehlgeschlagen',str(e),parent=window)
        ttk.Button(window,text='Notiz speichern',command=save,style='Accent.TButton').pack(anchor='e',padx=16,pady=(0,16))
        title.focus_set()

    def ai(self):
        return self.settings.ai()

    def present_answer(self, result):
        """Summary as (text, sources); a question as AnswerResult with its evidence state (step 7.4)."""
        from evidence import INSUFFICIENT, SUFFICIENT, UNAVAILABLE
        if isinstance(result, tuple):
            answer,sources = result
            self.evidence_notice.set('')
        else:
            answer,sources = result.text,result.sources
            self.evidence_notice.set(result.label)
            self.evidence_label.configure(style='TLabel' if result.state==SUFFICIENT else 'Warning.TLabel')
        if not isinstance(result, tuple) and result.state in (INSUFFICIENT, UNAVAILABLE):
            text = answer
            if sources:  # search hits as hints, never as an answer
                text += '\n\nÄHNLICHSTE DOKUMENTE (Suchhinweise, keine Antwort)\n'
                text += ''.join(f'\n· {s["title"]} · Dokument {s["document_id"]} · Abschnitt {s["ordinal"]}' for s in sources)
        else:
            text = answer+'\n\nQUELLEN\n'
            for i,s in enumerate(sources):
                text += f'\n[Q{i+1}] {s["title"]} · Dokument {s["document_id"]} · Abschnitt {s["ordinal"]}\n{s["content"]}\n'
        self.write(self.answer,text)
        self.tabs.select(self.chat)
        self.show_history()

    def ask(self):
        question = self.question.get().strip()
        if not question:
            self.status.set('Zuerst eine Frage eingeben.')
            return
        self.work(lambda:self.ai().ask(question),self.present_answer)

    def summarize(self):
        try:
            doc_id = self.selected()
            self.work(lambda:self.ai().summarize(doc_id),self.present_answer)
        except ValueError as e:
            messagebox.showinfo('Dokument auswählen',str(e),parent=self)

    def show_history(self):
        items = self.store.history()
        text = []
        from core import run_sources
        from evidence import LABELS
        for item in items:
            sources,details = run_sources(item['sources_json'])
            refs = '\n'.join(f'[Q{i+1}] {s["title"]} · Dokument {s["document_id"]} · Abschnitt {s["ordinal"]}'
                             + (f'\n{s["content"]}' if s.get('content') else '') for i,s in enumerate(sources))
            label = f' · {LABELS[details["evidence"]]}' if details.get('evidence') in LABELS else ''
            model = item['model'] or 'keine KI-Anfrage'
            text.append(f'{item["created_at"]} UTC · {model}{label}\n{item["question"]}\n\n{item["answer"]}\n\nQuellen zum Anfragezeitpunkt:\n{refs}')
        self.write(self.history,'\n\n'.join(text) if text else 'Noch keine KI-Anfragen gespeichert.')

    def open_settings(self):
        self.tabs.select(self.settings_tab)
        self.refresh_settings()

    def open_import(self):
        self.tabs.select(self.import_tab)
        self.refresh_import_accounts()

    def build_import(self):
        frame=self.import_tab
        ttk.Label(frame,text='Daten importieren',font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Importierte Inhalte werden als durchsuchbare Dokumente in der aktiven SQL-Datenbank gespeichert.',wraplength=900).pack(anchor='w',pady=(5,14))
        local=ttk.LabelFrame(frame,text='Lokale Dokumente',padding=14)
        local.pack(fill='x',pady=(0,10))
        ttk.Label(local,text='TXT, Markdown, PDF, DOCX, HTML, RTF, XLSX, PPTX, EML, mit OCR auch Bilder · maximal 50 MB je Datei · '
                  'Importe laufen als Auftrag und lassen sich abbrechen und fortsetzen').pack(anchor='w')
        local_buttons=ttk.Frame(local)
        local_buttons.pack(anchor='w',pady=(8,0))
        b=ttk.Button(local_buttons,text='Dokumente auswählen',command=self.import_files)
        b.pack(side='left',padx=(0,8)); self.buttons.append(b)
        self.folder_import_button=ttk.Button(local_buttons,text='Ordner inkl. Unterordner',command=self.import_local_folder,style='Accent.TButton')
        self.folder_import_button.pack(side='left',padx=(0,8)); self.buttons.append(self.folder_import_button)
        b=ttk.Button(local_buttons,text='Auftrag fortsetzen',command=self.resume_import_job)
        b.pack(side='left',padx=(0,8)); self.buttons.append(b)
        b=ttk.Button(local_buttons,text='Bericht speichern',command=self.save_import_report)
        b.pack(side='left',padx=(0,8)); self.buttons.append(b)
        # Not in self.buttons: it must stay usable while the import job runs.
        self.import_cancel_button=ttk.Button(local_buttons,text='Import abbrechen',command=self.cancel_import_job,state='disabled')
        self.import_cancel_button.pack(side='left')
        ocr_row=ttk.Frame(local)
        ocr_row.pack(anchor='w',fill='x',pady=(8,0))
        b=ttk.Button(ocr_row,text='Quarantäne erneut verarbeiten',command=self.retry_no_text)
        b.pack(side='left',padx=(0,8)); self.buttons.append(b)
        self.ocr_notice=tk.StringVar(value=self.ocr_status_text())
        ttk.Label(ocr_row,textvariable=self.ocr_notice,wraplength=760).pack(side='left',fill='x',expand=True)
        pst=ttk.LabelFrame(frame,text='Outlook-Archiv',padding=14)
        pst.pack(fill='x',pady=(0,10))
        ttk.Label(pst,text='PST-Datei lokal auslesen; Outlook-Installation ist nicht erforderlich.').pack(side='left')
        b=ttk.Button(pst,text='PST-Datei importieren',command=self.import_pst_file)
        b.pack(side='right'); self.buttons.append(b)
        mailbox=ttk.LabelFrame(frame,text='E-Mail-Konto',padding=14)
        mailbox.pack(fill='x',pady=(0,10))
        mailbox.columnconfigure(1,weight=1)
        ttk.Label(mailbox,text='Konto').grid(row=0,column=0,sticky='w',padx=(0,14),pady=5)
        self.import_account_value=tk.StringVar()
        self.import_account=ttk.Combobox(mailbox,textvariable=self.import_account_value,state='readonly')
        self.import_account.grid(row=0,column=1,sticky='ew',pady=5)
        ttk.Button(mailbox,text='Konten verwalten',command=self.open_mail_settings).grid(row=0,column=2,padx=(10,0),pady=5)
        ttk.Label(mailbox,text='Ordner').grid(row=1,column=0,sticky='w',padx=(0,14),pady=5)
        self.import_folder=tk.StringVar(value='INBOX')
        ttk.Entry(mailbox,textvariable=self.import_folder).grid(row=1,column=1,sticky='ew',pady=5)
        ttk.Label(mailbox,text='Max. E-Mails').grid(row=2,column=0,sticky='w',padx=(0,14),pady=5)
        self.import_limit=tk.StringVar(value='100')
        ttk.Spinbox(mailbox,from_=1,to=2000,textvariable=self.import_limit,width=10).grid(row=2,column=1,sticky='w',pady=5)
        b=ttk.Button(mailbox,text='Postfach importieren',command=self.import_mailbox,style='Accent.TButton')
        b.grid(row=2,column=2,padx=(10,0),pady=5); self.buttons.append(b)
        self.import_message=tk.StringVar(value='Bereit. E-Mail-Import liest nur und verändert keine Nachrichten auf dem Server.')
        ttk.Label(frame,textvariable=self.import_message,wraplength=1000).pack(anchor='w',fill='x',pady=(8,0))

    def refresh_import_accounts(self):
        rows=self.mail_accounts.list()
        self.import_account_ids={f'{r["label"]} · {r["email"]}':r['id'] for r in rows}
        values=list(self.import_account_ids)
        self.import_account.configure(values=values)
        if self.import_account_value.get() not in self.import_account_ids:
            self.import_account_value.set(values[0] if values else '')

    @staticmethod
    def report_text(report):
        text=f'{report["found"]} gefunden · {report["imported"]} importiert · {report["duplicates"]} bereits vorhanden · {report["errors"]} Fehler'
        counts=[report.get(key,0) for key in ('attachments_imported','attachments_duplicates',
                                               'attachments_quarantined','attachments_errors')]
        if any(counts):
            text+=(f' · Anhänge: {counts[0]} importiert, {counts[1]} bereits vorhanden, '
                   f'{counts[2]} in Quarantäne, {counts[3]} Fehler')
        quarantine=report.get('quarantine') or []
        if quarantine:
            text+=' ('+'; '.join(f'{entry["name"]}: {REASON_LABELS.get(entry["reason"],entry["reason"])}'
                                  for entry in quarantine[:5])+(' …' if len(quarantine)>5 else '')+')'
        return text

    def import_pst_file(self):
        path=filedialog.askopenfilename(title='Outlook-PST-Datei auswählen',filetypes=[('Outlook-PST','*.pst')])
        if not path: return
        try: limit=int(self.import_limit.get())
        except ValueError:
            messagebox.showerror('PST-Import','Gültige maximale Anzahl eingeben.',parent=self); return
        self.import_message.set('PST-Vorschau wird geladen …')
        label='PST · '+Path(path).stem
        self.work(lambda:self.importer.preview_pst(path,limit),
                  lambda items:self.show_mail_preview(items,None,path,label,
                      create_job=lambda selected,released:self.importer.create_pst_job(self.import_jobs,path,selected,released)))

    def import_mailbox(self):
        account_id=self.import_account_ids.get(self.import_account_value.get())
        if not account_id:
            messagebox.showinfo('E-Mail-Import','Zuerst unter Einstellungen ein E-Mail-Konto anlegen.',parent=self); return
        try: limit=int(self.import_limit.get())
        except ValueError:
            messagebox.showerror('E-Mail-Import','Gültige maximale Anzahl eingeben.',parent=self); return
        folder=self.import_folder.get().strip() or 'INBOX'
        account_name=self.import_account_value.get()
        self.import_message.set('E-Mail-Vorschau wird geladen …')
        self.work(lambda:self.importer.preview_account(account_id,folder,limit),
                  lambda items:self.show_mail_preview(items,account_id,folder,account_name))

    def show_mail_preview(self,items,account_id,folder,account_name='E-Mail-Konto',create_job=None):
        window=tk.Toplevel(self)
        window.title('E-Mails für Import auswählen')
        window.geometry('1080x650')
        window.minsize(820,480)
        window.transient(self)
        ttk.Label(window,text='E-Mails für Import auswählen',font=('Segoe UI',18,'bold'),padding=(18,16,18,0)).pack(anchor='w')
        ttk.Label(window,text=account_name+' · '+folder+' · Vollständige Inhalte werden erst nach der Freigabe geladen. '
                  'Anhänge werden nur geladen, wenn du sie einzeln freigibst.',
                  padding=(18,5,18,12),wraplength=1000).pack(anchor='w')
        body=ttk.Frame(window,padding=(18,0,18,8)); body.pack(fill='both',expand=True)
        columns=('active','status','date','sender','subject')
        tree=ttk.Treeview(body,columns=columns,show='tree headings',selectmode='extended')
        tree.column('#0',width=28,stretch=False)
        for col,title,width in [('active','Auswahl',75),('status','Status',120),('date','Datum',165),('sender','Absender',245),('subject','Betreff / Anhang',380)]:
            tree.heading(col,text=title); tree.column(col,width=width,stretch=col not in ('active','status'))
        scroll=ttk.Scrollbar(body,orient='vertical',command=tree.yview); tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); tree.pack(fill='both',expand=True)
        window.preview_tree=tree
        window.preview_items={}
        for index,item in enumerate(items):
            iid='mail-'+str(index)
            imported=bool(item.get('imported'))
            window.preview_items[iid]={'item':item,'active':False,'imported':imported,'attachment':None,'parent':None}
            tree.insert('','end',iid=iid,values=('NEIN','✓ Importiert' if imported else '',item.get('date',''),item.get('sender',''),item.get('subject','')),
                        tags=('imported',) if imported else (),open=bool(item.get('attachments')))
            for number,attachment in enumerate(item.get('attachments') or ()):
                child=f'{iid}:{number}'
                done=bool(attachment.get('imported'))
                window.preview_items[child]={'item':item,'active':False,'imported':done,'attachment':attachment,'parent':iid}
                label=(f'{attachment.get("name","Anhang")} · {attachment.get("content_type") or "unbekannt"} · '
                       f'{self.file_size_text(attachment.get("size"))}')
                tree.insert(iid,'end',iid=child,values=('NEIN','✓ Importiert' if done else '','','Anhang',label),
                            tags=('imported',) if done else ())
        tree.tag_configure('active',foreground=self.palette['accent'],font=('Segoe UI',11,'bold'))
        tree.tag_configure('imported',foreground=self.palette['success'],font=('Segoe UI',11,'bold'))
        tree.bind('<Double-1>',lambda event:self.toggle_preview_at(window,event))
        controls=ttk.Frame(window,padding=(18,4,18,4)); controls.pack(fill='x')
        ttk.Button(controls,text='Alle aktivieren',command=lambda:self.set_preview_active(window,True)).pack(side='left',padx=(0,8))
        ttk.Button(controls,text='Alle deaktivieren',command=lambda:self.set_preview_active(window,False)).pack(side='left',padx=(0,8))
        ttk.Button(controls,text='Markierte umschalten',command=lambda:self.toggle_preview_selected(window)).pack(side='left')
        window.preview_status=tk.StringVar()
        window.preview_status_label=ttk.Label(window,textvariable=window.preview_status,padding=(18,6,18,4))
        window.preview_status_label.pack(anchor='w')
        actions=ttk.Frame(window,padding=(18,4,18,16)); actions.pack(fill='x')
        window.import_button=ttk.Button(actions,text='Aktivierte E-Mails importieren',style='Accent.TButton',
                                        command=lambda:self.authorize_mail_import(window,account_id,folder))
        window.import_button.pack(side='right')
        ttk.Button(actions,text='Abbrechen',command=lambda:self.close_mail_preview(window)).pack(side='right',padx=(0,8))
        self.buttons.append(window.import_button)
        for widget in (actions,window.preview_status_label,controls):  # bottom up, before the list
            widget.pack_configure(side='bottom',before=body)
        window.protocol('WM_DELETE_WINDOW',lambda:self.close_mail_preview(window))
        window.create_job=create_job or (lambda selected,released:self.importer.create_mail_job(
            self.import_jobs,account_id,folder,selected,released))
        self.mail_preview_window=window
        self.update_preview_status(window)
        self.import_message.set(f'{len(items)} E-Mails zur Auswahl geladen; noch keine E-Mail ist zum Import aktiviert.')
        return window

    def update_preview_status(self,window):
        mails=[row for row in window.preview_items.values() if row['attachment'] is None]
        attachments=[row for row in window.preview_items.values() if row['attachment'] is not None]
        active=sum(row['active'] for row in mails)
        imported=sum(row['imported'] for row in mails)
        released=sum(row['active'] for row in attachments)
        window.preview_status.set(f'{active} zum Import aktiviert · {imported} bereits importiert · '
                                  f'{released} von {len(attachments)} Anhängen freigegeben · {len(mails)} angezeigt.')

    def _show_preview_row(self,window,iid):
        row=window.preview_items[iid]
        values=list(window.preview_tree.item(iid,'values')); values[0]='JA' if row['active'] else 'NEIN'
        tags=('imported',) if row['imported'] else (('active',) if row['active'] else ())
        window.preview_tree.item(iid,values=values,tags=tags)

    def _set_preview_row(self,window,iid,active):
        row=window.preview_items[iid]
        if row['imported']:
            return
        row['active']=active
        self._show_preview_row(window,iid)
        if row['attachment'] is not None and active:  # a released attachment brings its e-mail along
            parent=window.preview_items[row['parent']]
            if not parent['imported'] and not parent['active']:
                parent['active']=True; self._show_preview_row(window,row['parent'])
        if row['attachment'] is None and not active:  # without its e-mail, no attachment
            for child in window.preview_tree.get_children(iid):
                if window.preview_items[child]['active']:
                    window.preview_items[child]['active']=False; self._show_preview_row(window,child)

    def set_preview_active(self,window,active):
        """Applies to e-mails only. Attachments are always released one by one."""
        for iid,row in window.preview_items.items():
            if row['attachment'] is None:
                self._set_preview_row(window,iid,active)
        self.update_preview_status(window)

    def toggle_preview_selected(self,window,iids=None):
        for iid in (iids or window.preview_tree.selection()):
            row=window.preview_items.get(iid)
            if not row or row['imported']: continue
            self._set_preview_row(window,iid,not row['active'])
        self.update_preview_status(window)

    def toggle_preview_at(self,window,event):
        iid=window.preview_tree.identify_row(event.y)
        if iid:
            self.toggle_preview_selected(window,(iid,))

    def authorize_mail_import(self,window,account_id,folder):
        selected=[row['item'] for row in window.preview_items.values() if row['active'] and row['attachment'] is None]
        released={}
        for row in window.preview_items.values():
            if row['active'] and row['attachment'] is not None:
                released.setdefault(row['item']['id'],[]).append(row['attachment']['id'])
        if not selected and not released:
            messagebox.showinfo('E-Mail-Import','Mindestens eine E-Mail oder einen Anhang aktivieren.',parent=window); return
        # The selection becomes an import job: progress, cancel, resume and report as for files.
        self.run_import_job(lambda:window.create_job(selected,released))

    def close_mail_preview(self,window):
        button=getattr(window,'import_button',None)
        if button in self.buttons: self.buttons.remove(button)
        if window.winfo_exists(): window.destroy()

    def open_mail_settings(self):
        self.tabs.select(self.settings_tab)
        self.select_settings_page(self.mail_settings_frame)
        self.refresh_mail_accounts()

    def open_database_settings(self):
        self.tabs.select(self.settings_tab)
        self.select_settings_page(self.database_settings_frame)
        self.load_database_settings()

    def open_design_settings(self):
        self.tabs.select(self.settings_tab)
        self.select_settings_page(self.design_settings_frame)
        self.refresh_design_settings()

    def add_scrollable_settings_page(self,title):
        """Add a settings tab whose content scrolls vertically; returns the content frame."""
        page=ttk.Frame(self.settings_sections)
        canvas=tk.Canvas(page,highlightthickness=0,borderwidth=0,bg=self.palette['background'])
        scrollbar=ttk.Scrollbar(page,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True)
        content=ttk.Frame(canvas,padding=12)
        item=canvas.create_window((0,0),window=content,anchor='nw')
        content.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        wrap_limits={}
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def button_row(frame):
            children=frame.winfo_children()
            return (frame.winfo_class()=='TFrame' and len(children)>1
                    and all(child.winfo_class()=='TButton' for child in children)
                    and all(child.winfo_manager()=='grid' or (child.winfo_manager()=='pack'
                            and child.pack_info().get('side')=='left') for child in children))
        def flow(frame,available):
            # Lay out a row of left-aligned buttons in as many lines as the width needs.
            buttons=frame.winfo_children()
            for button in buttons:
                if button.winfo_manager()=='pack': button.pack_forget()
            row=column=used=0
            for button in buttons:
                needed=button.winfo_reqwidth()+8
                if column and used+needed>available:
                    row,column,used=row+1,0,0
                button.grid(row=row,column=column,sticky='w',padx=(0,8),pady=(6 if row else 0,0))
                column,used=column+1,used+needed
        def fit(_event=None):
            # Content is exactly as wide as the visible page: text wraps (never wider than
            # its designed limit) and button rows break into further lines when needed.
            content.update_idletasks()
            width=canvas.winfo_width()
            if width<=1: return
            canvas.itemconfigure(item,width=width)
            content.update_idletasks()
            for widget in descendants(content):
                if not widget.winfo_ismapped(): continue
                available=max(200,width-(widget.winfo_rootx()-content.winfo_rootx())-24)
                if widget.winfo_class()=='TLabel':
                    limit=wrap_limits.setdefault(widget,int(str(widget.cget('wraplength') or 0)))
                    widget.configure(wraplength=min(limit,available) if limit else available)
                elif button_row(widget):
                    flow(widget,available)
        canvas.bind('<Configure>',lambda _:self.refit_settings_page())
        self.settings_fitters[str(page)]=fit
        def wheel(event):
            if event.num==4 or event.delta>0: canvas.yview_scroll(-3,'units')
            elif event.num==5 or event.delta<0: canvas.yview_scroll(3,'units')
        # Bind the wheel only while the pointer is over this page, so other tabs keep theirs.
        page.bind('<Enter>',lambda _:[self.bind_all(name,wheel) for name in ('<MouseWheel>','<Button-4>','<Button-5>')])
        page.bind('<Leave>',lambda _:[self.unbind_all(name) for name in ('<MouseWheel>','<Button-4>','<Button-5>')])
        self.settings_sections.add(page,text=title)
        self.settings_canvases.append(canvas)
        return content

    def refit_settings_page(self):
        """Coalesce resize and tab events into one refit of the visible settings page."""
        if self.settings_refit_pending: return
        self.settings_refit_pending=True
        def run():
            self.settings_refit_pending=False
            self.settings_fitters.get(self.settings_sections.select(),lambda:None)()
        self.after_idle(run)

    def select_settings_page(self,content):
        self.settings_sections.select(content.master.master)

    def build_settings(self):
        self.settings_sections=ttk.Notebook(self.settings_tab)
        self.settings_sections.pack(fill='both',expand=True)
        self.settings_canvases=[]
        self.settings_fitters={}
        self.settings_refit_pending=False
        self.settings_sections.bind('<<NotebookTabChanged>>',lambda _:self.refit_settings_page())
        # A canvas does not always receive <Configure> for the last step of a window resize,
        # so the toplevel's own resize also refits the visible settings page.
        self.bind('<Configure>',lambda event:self.refit_settings_page() if event.widget is self else None,add='+')
        self.ai_settings_frame=self.add_scrollable_settings_page('KI-Anbieter')
        self.database_settings_frame=self.add_scrollable_settings_page('Datenbank')
        self.mail_settings_frame=self.add_scrollable_settings_page('E-Mail-Konten')
        self.rules_settings_frame=self.add_scrollable_settings_page('Regeln')
        self.design_settings_frame=self.add_scrollable_settings_page('Design')
        self.configuration_settings_frame=self.add_scrollable_settings_page('Konfiguration')
        frame=self.ai_settings_frame
        ttk.Label(frame,text='KI-Anbieter & API-Schlüssel',font=('Segoe UI',18,'bold')).pack(anchor='w')
        self.active_notice=tk.StringVar()
        ttk.Label(frame,textvariable=self.active_notice).pack(anchor='w',pady=(6,12))
        columns=('active','name','model','key','source')
        self.provider_tree=ttk.Treeview(frame,columns=columns,show='headings',height=4,selectmode='browse')
        for col,title,width in [('active','Verwendung',90),('name','KI-Anbieter',175),('model','Modell',150),
                                ('key','API-Schlüssel (maskiert)',165),('source','Herkunft / Status',255)]:
            self.provider_tree.heading(col,text=title)
            self.provider_tree.column(col,width=width)
        self.provider_tree.tag_configure('active',foreground='#185b50',font=('Segoe UI',11,'bold'))
        self.provider_tree.pack(fill='x')
        self.provider_tree.bind('<<TreeviewSelect>>',lambda _:self.load_provider_form())
        form=ttk.Frame(frame,padding=(0,12,0,6))
        form.pack(fill='x')
        form.columnconfigure(1,weight=1)
        ttk.Label(form,text='Ausgewählter Anbieter').grid(row=0,column=0,sticky='w',padx=(0,20),pady=6)
        self.provider_name=tk.StringVar()
        ttk.Label(form,textvariable=self.provider_name,font=('Segoe UI',12,'bold')).grid(row=0,column=1,sticky='w')
        ttk.Label(form,text='Modell-ID').grid(row=1,column=0,sticky='w',padx=(0,20),pady=6)
        self.model_value=tk.StringVar()
        self.model_entry=ttk.Combobox(form,textvariable=self.model_value)
        self.model_entry.grid(row=1,column=1,sticky='ew',pady=6)
        ttk.Label(form,text='API-Schlüssel').grid(row=2,column=0,sticky='w',padx=(0,20),pady=6)
        self.key_value=tk.StringVar()
        self.key_entry=ttk.Entry(form,textvariable=self.key_value,show='*')
        self.key_entry.grid(row=2,column=1,sticky='ew',pady=6)
        self.key_hint=ttk.Label(form,text='Vorhandene Schlüssel sind vorausgefüllt und maskiert. Zum Ersetzen einen neuen Schlüssel eintragen.')
        self.key_hint.grid(row=3,column=1,sticky='w',pady=(2,5))
        actions=ttk.Frame(frame)
        actions.pack(fill='x',pady=(2,6))
        for title,command,style in [('Einstellungen speichern',self.save_provider,'TButton'),
                                   ('Als aktive KI verwenden',self.activate_provider,'Accent.TButton'),
                                   ('Verbindung testen',self.test_provider,'TButton'),
                                   ('Schlüssel entfernen',self.remove_provider_key,'TButton'),
                                   ('Schlüsseldatei importieren',self.select_key,'TButton')]:
            b=ttk.Button(actions,text=title,command=command,style=style)
            b.pack(side='left',padx=(0,8))
            self.buttons.append(b)
        self.settings_message=tk.StringVar()
        self.settings_status=ttk.Label(frame,textvariable=self.settings_message)
        self.settings_status.pack(anchor='w',pady=(4,5))
        self.settings_help=ttk.Label(frame,text='Modell-IDs sind bearbeitbar; Modellzugriff hängt vom Anbieter ab. Verbindungstests können API-Kosten verursachen. '
                  'Schlüssel werden nicht in der Wissensdatenbank gespeichert. Entfernen deaktiviert auch vorhandene Datei-/Umgebungsschlüssel für diese Anwendung.')
        self.settings_help.pack(anchor='w',fill='x',pady=(5,0))
        self.vector_panel=VectorPanel(self,frame)
        self.refresh_settings()
        self.build_database_settings()
        self.build_mail_settings()
        self.build_design_settings()
        self.build_configuration_settings()
        self.rules_page=RulesPage(self,self.rules_settings_frame)

    def build_configuration_settings(self):
        frame=self.configuration_settings_frame
        ttk.Label(frame,text='Konfiguration übertragen',font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Exportiert KI-Auswahl und Modelle, Datenbankprofil, Designmodus sowie E-Mail-Kontometadaten. '
                  'API-Schlüssel, Kennwörter, Token, Dokumente, Logs und Logo-Dateien werden niemals übernommen.',
                  wraplength=940).pack(anchor='w',pady=(8,16))
        actions=ttk.Frame(frame); actions.pack(fill='x')
        for title,command,style in [('Konfiguration exportieren',self.export_configuration,'Accent.TButton'),
                                   ('Konfiguration importieren',self.import_configuration,'TButton')]:
            button=ttk.Button(actions,text=title,command=command,style=style)
            button.pack(side='left',padx=(0,8)); self.buttons.append(button)
        ttk.Label(frame,text='Ein importierter Wechsel der Datenquelle gilt erst nach einem Programmneustart. '
                  'Vorhandene Zugangsdaten im Betriebssystem-Schlüsselspeicher bleiben unverändert.',
                  wraplength=940).pack(anchor='w',pady=(16,0))

    def export_configuration(self):
        try:
            path=filedialog.asksaveasfilename(title='Konfiguration exportieren',defaultextension='.json',
                                              filetypes=[('JSON-Konfiguration','*.json')])
            if not path:
                return
            self.configuration_transfer.export_file(path)
            messagebox.showinfo('Konfiguration exportieren',
                                'Konfiguration wurde ohne Schlüssel, Kennwörter und Token exportiert.',parent=self)
        except ConfigurationTransferError as error:
            messagebox.showerror('Konfiguration exportieren',str(error),parent=self)
        except Exception:
            messagebox.showerror('Konfiguration exportieren',EXPORT_ERROR,parent=self)

    def import_configuration(self):
        path=filedialog.askopenfilename(title='Konfiguration importieren',
                                        filetypes=[('JSON-Konfiguration','*.json')])
        if not path:
            return
        try:
            result=self.configuration_transfer.import_file(path)
        except ConfigurationTransferError as error:
            messagebox.showerror('Konfiguration importieren',str(error),parent=self)
            return
        except Exception:
            messagebox.showerror('Konfiguration importieren',APPLY_ERROR,parent=self)
            return
        try:
            self.refresh_settings()
            self.load_database_settings()
            self.refresh_mail_accounts()
            self.refresh_design_settings()
            self.apply_theme(self.design_settings.theme())
        except Exception:
            messagebox.showwarning(
                'Konfiguration importieren',
                'Konfiguration wurde importiert, aber die Anzeige konnte nicht aktualisiert werden. Programm neu starten.',
                parent=self)
            return
        text='Konfiguration wurde importiert. Vorhandene Zugangsdaten blieben unverändert.'
        if result['restart_required']:
            text+=' Programm neu starten, damit die konfigurierte Datenquelle verwendet wird.'
        messagebox.showinfo('Konfiguration importieren',text,parent=self)

    def build_design_settings(self):
        frame=self.design_settings_frame
        ttk.Label(frame,text='Design & Branding',font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Telekom CI',style='BrandTitle.TLabel').pack(anchor='w',pady=(8,2))
        ttk.Label(frame,text='Telekom Magenta wird für Markenleiste, aktive Registerkarten, Auswahlzustände und primäre Aktionen verwendet.',
                  wraplength=940).pack(anchor='w',pady=(0,16))
        mode=ttk.LabelFrame(frame,text='Darstellung',padding=14)
        mode.pack(fill='x',pady=(0,12))
        self.design_theme=tk.StringVar(value=self.theme_mode)
        ttk.Radiobutton(mode,text='Hellmodus',value='light',variable=self.design_theme,
                        command=self.save_design_theme).pack(side='left',padx=(0,24))
        ttk.Radiobutton(mode,text='Dunkelmodus',value='dark',variable=self.design_theme,
                        command=self.save_design_theme).pack(side='left')
        logo=ttk.LabelFrame(frame,text='Logo auf der Hauptseite',padding=14)
        logo.pack(fill='x',pady=(0,12))
        logo.columnconfigure(1,weight=1)
        self.design_logo_preview=ttk.Label(logo,text='Kein Logo hinterlegt',anchor='center')
        self.design_logo_preview.grid(row=0,column=0,rowspan=2,sticky='w',padx=(0,24))
        ttk.Label(logo,text='PNG, JPG oder WebP · maximal 5 MB · automatische Skalierung auf maximal 320 × 96 Pixel',
                  wraplength=700).grid(row=0,column=1,sticky='w')
        actions=ttk.Frame(logo)
        actions.grid(row=1,column=1,sticky='w',pady=(10,0))
        for title,command,style in [('Logo auswählen',self.select_design_logo,'Accent.TButton'),
                                   ('Logo entfernen',self.remove_design_logo,'TButton')]:
            button=ttk.Button(actions,text=title,command=command,style=style)
            button.pack(side='left',padx=(0,8)); self.buttons.append(button)
        self.design_message=tk.StringVar()
        ttk.Label(frame,textvariable=self.design_message,style='Muted.TLabel').pack(anchor='w',pady=(4,0))
        self.refresh_design_settings()

    def refresh_design_settings(self):
        if hasattr(self,'design_theme'):
            self.design_theme.set(self.design_settings.theme())
            state=(self.logo_load_error or ('Logo hinterlegt und auf der Hauptseite sichtbar.'
                   if self.design_settings.logo_path.is_file() else 'Kein eigenes Logo hinterlegt.'))
            self.design_message.set(state+' Designmodus: '+('Dunkel' if self.design_settings.theme()=='dark' else 'Hell')+'.')
        self.refresh_logo()

    def save_design_theme(self):
        try:
            theme=self.design_theme.get()
            self.design_settings.save_theme(theme)
            self.apply_theme(theme)
            self.design_message.set('Designmodus gespeichert: '+('Dunkel.' if theme=='dark' else 'Hell.'))
        except Exception as error:
            messagebox.showerror('Design',str(error),parent=self)

    def select_design_logo(self):
        path=filedialog.askopenfilename(title='Logo auswählen',
                filetypes=[('Bilddateien','*.png *.jpg *.jpeg *.webp'),('Alle Dateien','*')])
        if not path:
            return
        try:
            self.design_settings.save_logo(path)
            self.refresh_logo()
            self.design_message.set('Logo gespeichert und auf der Hauptseite aktiviert.')
        except Exception as error:
            messagebox.showerror('Logo',str(error),parent=self)

    def remove_design_logo(self):
        self.design_settings.remove_logo()
        self.refresh_logo()
        self.design_message.set('Logo entfernt. Die Hauptseite verwendet wieder die Wortmarke Wissensraum.')

    def build_database_settings(self):
        frame=self.database_settings_frame
        ttk.Label(frame,text='Datenbank',font=('Segoe UI',18,'bold')).pack(anchor='w')
        self.database_active=tk.StringVar()
        ttk.Label(frame,textvariable=self.database_active,font=('Segoe UI',11,'bold')).pack(anchor='w',pady=(5,12))
        form=ttk.LabelFrame(frame,text='PostgreSQL-Verbindung',padding=14)
        form.pack(fill='x'); form.columnconfigure(1,weight=1); form.columnconfigure(3,weight=1)
        self.database_vars={name:tk.StringVar() for name in ('host','port','database','username','password','sslmode')}
        fields=[('Server / IP-Adresse','host'),('Port','port'),('Datenbankname','database'),
                ('Benutzername','username'),('Kennwort','password'),('TLS-Modus','sslmode')]
        self.database_fields={}
        for index,(title,name) in enumerate(fields):
            row=index//2; column=(index%2)*2
            ttk.Label(form,text=title).grid(row=row,column=column,sticky='w',padx=(0,10),pady=6)
            if name=='sslmode':
                widget=ttk.Combobox(form,textvariable=self.database_vars[name],state='readonly',
                                    values=('require','verify-ca','verify-full'))
            else:
                widget=ttk.Entry(form,textvariable=self.database_vars[name],show='*' if name=='password' else '')
            widget.grid(row=row,column=column+1,sticky='ew',padx=(0,18),pady=6)
            self.database_fields[name]=widget
        ttk.Label(frame,text='Das Kennwort wird ausschließlich im Betriebssystem-Schlüsselspeicher abgelegt. '
                  'TLS ist für PostgreSQL-Verbindungen verpflichtend.',wraplength=950).pack(anchor='w',pady=(10,6))
        actions=ttk.Frame(frame); actions.pack(fill='x',pady=(4,6))
        for title,command,style in [('Einstellungen speichern',self.save_database_settings,'TButton'),
                                   ('Verbindung testen',self.test_database_connection,'TButton'),
                                   ('PostgreSQL aktivieren',self.activate_postgresql,'Accent.TButton'),
                                   ('Lokales SQLite aktivieren',self.activate_sqlite,'TButton')]:
            button=ttk.Button(actions,text=title,command=command,style=style)
            button.pack(side='left',padx=(0,8)); self.buttons.append(button)
        reset_actions=ttk.Frame(frame); reset_actions.pack(fill='x',pady=(4,2))
        button=ttk.Button(reset_actions,text='Anwendungsschema zurücksetzen',command=self.reset_postgresql)
        button.pack(side='left'); self.buttons.append(button)
        ttk.Label(reset_actions,text='Löscht alle Wissensraum-Daten in PostgreSQL.',style='Warning.TLabel').pack(side='left',padx=12)
        operations=ttk.Frame(frame); operations.pack(fill='x',pady=(4,2))
        for title,command in (('Sicherung wiederherstellen',self.restore),('Systemstatus prüfen',self.show_health)):
            button=ttk.Button(operations,text=title,command=command)
            button.pack(side='left',padx=(0,8)); self.buttons.append(button)
        ttk.Label(operations,text='Wiederherstellung nur für SQLite.').pack(side='left',padx=4)
        cleanup=ttk.Frame(frame); cleanup.pack(fill='x',pady=(4,2))
        button=ttk.Button(cleanup,text='Anhänge aus alten E-Mails entfernen',command=self.strip_old_mail_attachments)
        button.pack(side='left',padx=(0,8)); self.buttons.append(button)
        ttk.Label(cleanup,text='Für IMAP-E-Mails, die vor Release 2.0 importiert wurden.').pack(side='left',padx=4)
        self.database_message=tk.StringVar()
        ttk.Label(frame,textvariable=self.database_message,wraplength=950).pack(anchor='w',pady=(5,4))
        ttk.Label(frame,text='Eine Aktivierung gilt ab dem nächsten Programmstart. Vorhandene SQLite-Dokumente werden nicht automatisch '
                  'nach PostgreSQL übertragen. Beide Datenbestände bleiben getrennt.',wraplength=950).pack(anchor='w',pady=(6,0))
        self.load_database_settings()

    def database_form(self):
        return {name:value.get() for name,value in self.database_vars.items() if name!='password'}

    def load_database_settings(self):
        profile=self.database_settings.profile()
        for name in ('host','port','database','username','sslmode'):
            self.database_vars[name].set(profile[name])
        password,source=self.database_settings.password()
        self.database_vars['password'].set(password)
        configured='PostgreSQL auf dem VPS' if profile['backend']=='postgresql' else 'Lokale SQLite-Datei'
        running='PostgreSQL' if self.store.backend=='postgresql' else 'SQLite'
        self.database_active.set('Konfiguriert: '+configured+' · Aktuelle Sitzung: '+running)
        self.database_message.set('Kennwort: '+source)

    def save_database_settings(self):
        try:
            self.database_settings.save(self.database_form(),self.database_vars['password'].get())
            self.load_database_settings()
            self.database_message.set('PostgreSQL-Verbindungsdaten gespeichert. Aktive Datenquelle bleibt unverändert.')
        except Exception as error:
            messagebox.showerror('Datenbank',str(error),parent=self)

    def test_database_connection(self):
        values=self.database_form(); password=self.database_vars['password'].get()
        def done(result):
            self.database_message.set('Verbindung erfolgreich: '+result['database']+' · Benutzer '+result['username']+' · '+result['version'])
        self.work(lambda:self.database_settings.test(values,password),done)

    def activate_postgresql(self):
        values=self.database_form(); password=self.database_vars['password'].get()
        def done(_):
            self.load_database_settings()
            self.database_message.set('PostgreSQL ist aktiviert. Programm neu starten; danach werden neue Daten auf dem VPS gespeichert.')
        self.work(lambda:self.database_settings.activate_postgresql(values,password),done)

    def activate_sqlite(self):
        try:
            self.database_settings.activate_sqlite()
            self.load_database_settings()
            self.database_message.set('Lokales SQLite ist aktiviert. Programm neu starten, um die Datenquelle zu wechseln.')
        except Exception as error:
            messagebox.showerror('Datenbank',str(error),parent=self)

    def reset_postgresql(self):
        database=self.database_vars['database'].get().strip()
        confirmation=simpledialog.askstring('Anwendungsschema zurücksetzen',
            'Alle Wissensraum-Dokumente, KI-Verläufe, Einstellungen, E-Mail-Konten und Importmarkierungen in PostgreSQL werden gelöscht.\n\n'
            'Zum Bestätigen den Datenbanknamen eingeben:',parent=self)
        if confirmation is None:
            return
        values=self.database_form(); password=self.database_vars['password'].get()
        def done(result):
            self.refresh(); self.show_history(); self.refresh_mail_accounts()
            self.database_message.set('Schema wissensraum in '+result['database']+' wurde zurückgesetzt und mit '
                                      +str(len(result['tables']))+' Tabellen neu angelegt.')
        self.work(lambda:self.database_settings.reset_postgresql(values,password,confirmation),done)

    def build_mail_settings(self):
        frame=self.mail_settings_frame
        ttk.Label(frame,text='E-Mail-Konten',font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Kontodaten werden in der aktiven SQL-Datenbank gespeichert; Kennwörter und OAuth-Token im Betriebssystem-Schlüsselspeicher.',wraplength=950).pack(anchor='w',pady=(5,10))
        columns=('label','provider','email','status')
        self.mail_tree=ttk.Treeview(frame,columns=columns,show='headings',height=4,selectmode='browse')
        for col,title,width in [('label','Kontoname',180),('provider','Anbieter',190),('email','E-Mail-Adresse',260),('status','Anmeldung',150)]:
            self.mail_tree.heading(col,text=title); self.mail_tree.column(col,width=width)
        self.mail_tree.pack(fill='x')
        self.mail_tree.bind('<<TreeviewSelect>>',lambda _:self.load_mail_account())
        form=ttk.Frame(frame,padding=(0,10,0,5)); form.pack(fill='x'); form.columnconfigure(1,weight=1); form.columnconfigure(3,weight=1)
        self.mail_vars={name:tk.StringVar() for name in ('id','label','provider','email','username','host','port','client_id','tenant','secret')}
        self.mail_provider_names={info['name']:key for key,info in MAIL_PROVIDERS.items()}
        self.mail_provider_value=tk.StringVar()
        self.mail_provider=ttk.Combobox(form,textvariable=self.mail_provider_value,state='readonly',values=list(self.mail_provider_names))
        fields=[('Kontoname','label',ttk.Entry),('Anbieter','provider',None),('E-Mail-Adresse','email',ttk.Entry),
                ('Benutzername','username',ttk.Entry),('IMAP-Server','host',ttk.Entry),('IMAP-Port','port',ttk.Entry),
                ('Microsoft Client-ID','client_id',ttk.Entry),('Microsoft Mandant','tenant',ttk.Entry)]
        self.mail_fields={}
        for index,(title,name,kind) in enumerate(fields):
            row=index//2; col=(index%2)*2
            ttk.Label(form,text=title).grid(row=row,column=col,sticky='w',padx=(0,10),pady=4)
            widget=self.mail_provider if name=='provider' else kind(form,textvariable=self.mail_vars[name])
            widget.grid(row=row,column=col+1,sticky='ew',padx=(0,18),pady=4); self.mail_fields[name]=widget
        ttk.Label(form,text='Kennwort / App-Passwort').grid(row=4,column=0,sticky='w',padx=(0,10),pady=4)
        self.mail_secret=ttk.Entry(form,textvariable=self.mail_vars['secret'],show='*')
        self.mail_secret.grid(row=4,column=1,sticky='ew',padx=(0,18),pady=4)
        ttk.Label(form,text='Bestehende Zugangsdaten werden nicht angezeigt. Leer lassen, um sie beizubehalten.',wraplength=480).grid(row=4,column=2,columnspan=2,sticky='w',pady=4)
        self.mail_provider.bind('<<ComboboxSelected>>',lambda _:self.select_mail_provider())
        actions=ttk.Frame(frame); actions.pack(fill='x',pady=(4,6))
        for title,command,style in [('Neues Konto',self.new_mail_account,'TButton'),('Konto speichern',self.save_mail_account,'Accent.TButton'),
                                   ('Microsoft verbinden',self.connect_microsoft,'TButton'),('Konto löschen',self.delete_mail_account,'TButton')]:
            b=ttk.Button(actions,text=title,command=command,style=style); b.pack(side='left',padx=(0,8)); self.buttons.append(b)
            if command==self.connect_microsoft: self.microsoft_connect_button=b
        self.mail_message=tk.StringVar()
        ttk.Label(frame,textvariable=self.mail_message,wraplength=950).pack(anchor='w',pady=(5,0))
        self.sync_panel=SyncPanel(self,frame)
        self.refresh_mail_accounts()

    def refresh_mail_accounts(self,selected=None):
        rows=self.mail_accounts.list(); self.mail_rows={r['id']:r for r in rows}
        self.mail_tree.delete(*self.mail_tree.get_children())
        for row in rows:
            self.mail_tree.insert('','end',iid=row['id'],values=(row['label'],MAIL_PROVIDERS[row['provider']]['name'],row['email'],row['auth_status']))
        if selected in self.mail_rows:
            self.mail_tree.selection_set(selected); self.load_mail_account()
        elif rows:
            self.mail_tree.selection_set(rows[0]['id']); self.load_mail_account()
        else:
            self.new_mail_account()
        self.refresh_import_accounts()

    def new_mail_account(self):
        for value in self.mail_vars.values(): value.set('')
        self.mail_vars['provider'].set('gmail'); self.mail_vars['port'].set('993'); self.mail_vars['tenant'].set('common')
        self.apply_mail_provider(); self.mail_message.set('Neues Konto. Gmail benötigt in der Regel ein App-Passwort; Microsoft verwendet OAuth 2.0.')
        self.sync_panel.load()

    def load_mail_account(self):
        selected=self.mail_tree.selection()
        if not selected: return
        row=self.mail_rows[selected[0]]
        for name in ('id','label','provider','email','username','host','port','client_id','tenant'):
            self.mail_vars[name].set(row[name])
        self.mail_vars['secret'].set(''); self.apply_mail_provider()
        self.mail_message.set(row['auth_status']+' · Bestehende Zugangsdaten bleiben verborgen.')
        self.sync_panel.load()

    def apply_mail_provider(self):
        provider=self.mail_vars['provider'].get() or 'gmail'
        self.mail_provider_value.set(MAIL_PROVIDERS[provider]['name'])
        if provider=='gmail':
            self.mail_vars['host'].set('imap.gmail.com'); self.mail_vars['port'].set('993')
        elif provider=='microsoft':
            self.mail_vars['host'].set(''); self.mail_vars['port'].set('0'); self.mail_vars['tenant'].set(self.mail_vars['tenant'].get() or 'common')
        for name in ('host','port','username','secret'):
            self.mail_fields.get(name,self.mail_secret).configure(state='disabled' if provider=='microsoft' else 'normal')
        if provider=='gmail':  # Gmail uses the address as user name; the field only shows it
            self.mail_vars['username'].set(self.mail_vars['email'].get())
            self.mail_fields['username'].configure(state='disabled')
        for name in ('client_id','tenant'):
            self.mail_fields[name].configure(state='normal' if provider=='microsoft' else 'disabled')
        self.microsoft_connect_button.configure(state='normal' if provider=='microsoft' else 'disabled')

    def select_mail_provider(self):
        previous=self.mail_vars['provider'].get()
        provider=self.mail_provider_names[self.mail_provider_value.get()]
        self.mail_vars['provider'].set(provider)
        if provider=='imap' and previous!='imap':
            self.mail_vars['host'].set(''); self.mail_vars['port'].set('993')
        self.apply_mail_provider()

    def mail_form(self):
        return {name:value.get() for name,value in self.mail_vars.items() if name!='secret'}

    def save_mail_account(self):
        try:
            account_id=self.mail_accounts.save(self.mail_form(),self.mail_vars['secret'].get())
            self.refresh_mail_accounts(account_id); self.mail_message.set('E-Mail-Konto gespeichert.')
        except Exception as e:
            messagebox.showerror('E-Mail-Konto',str(e),parent=self)

    def connect_microsoft(self):
        account_id=self.mail_vars['id'].get()
        if not account_id or self.mail_vars['provider'].get()!='microsoft':
            messagebox.showinfo('Microsoft verbinden','Microsoft-Konto zuerst mit Client-ID speichern.',parent=self); return
        self.work(lambda:self.mail_accounts.begin_microsoft(account_id),self.show_microsoft_login)

    def show_microsoft_login(self,context):
        messagebox.showinfo('Microsoft-Anmeldung',context['message'],parent=self)
        self.work(lambda:self.mail_accounts.finish_microsoft(context),lambda _:(self.refresh_mail_accounts(context['account_id']),self.mail_message.set('Microsoft-Konto verbunden.')))

    def delete_mail_account(self):
        account_id=self.mail_vars['id'].get()
        if account_id and messagebox.askyesno('E-Mail-Konto','Konto und gespeicherte Zugangsdaten entfernen? Importierte E-Mails bleiben erhalten.',parent=self):
            try:
                self.mail_accounts.delete(account_id); self.refresh_mail_accounts(); self.mail_message.set('E-Mail-Konto entfernt.')
            except Exception as e:
                messagebox.showerror('E-Mail-Konto',str(e),parent=self)

    def selected_provider(self):
        selected=self.provider_tree.selection()
        return selected[0] if selected else self.settings.active()

    def refresh_settings(self,provider=None):
        selected=provider or (self.provider_tree.selection()[0] if self.provider_tree.selection() else self.settings.active())
        self.provider_tree.delete(*self.provider_tree.get_children())
        for row in self.settings.overview():
            self.provider_tree.insert('','end',iid=row['provider'],
                                      values=('AKTIV' if row['active'] else '',row['name'],row['model'],row['masked'],row['source']),
                                      tags=('active',) if row['active'] else ())
        self.provider_tree.selection_set(selected)
        self.load_provider_form()
        active=self.settings.active()
        self.active_notice.set('Aktive KI: '+PROVIDERS[active]['name']+' · '+self.settings.model(active))
        if active=='ollama':
            self.provider_notice.set('Aktive KI: Ollama auf diesem Rechner. Frage und Quellen gehen an den lokalen Ollama-Dienst.')
        else:
            self.provider_notice.set('Aktive KI: '+PROVIDERS[active]['name']+'. Frage und passende Textstellen werden an diesen Anbieter gesendet.')

    def load_provider_form(self):
        provider=self.selected_provider()
        info=PROVIDERS[provider]
        self.provider_name.set(info['name'])
        self.model_entry.configure(values=info['models'])
        self.model_value.set(self.settings.model(provider))
        key,source=self.settings.credential(provider)
        self.key_entry.configure(state='normal' if info['env'] else 'disabled')
        self.key_value.set(key)
        self.settings_message.set(source+(' · Ollama muss lokal installiert sein und das Modell bereitstellen.' if provider=='ollama' else ''))

    def save_provider(self):
        try:
            provider=self.selected_provider()
            key=(self.key_value.get().strip() or None) if PROVIDERS[provider]['env'] else None
            self.settings.save(provider,self.model_value.get(),key,activate=False)
            self.refresh_settings(provider)
            self.settings_message.set('Einstellungen gespeichert: '+PROVIDERS[provider]['name']+'. Die aktive KI bleibt unverändert.')
        except Exception as e:
            messagebox.showerror('Einstellungen',str(e),parent=self)

    def activate_provider(self):
        try:
            provider=self.selected_provider()
            key=(self.key_value.get().strip() or None) if PROVIDERS[provider]['env'] else None
            if PROVIDERS[provider]['env'] and not key:
                raise ValueError('Für diesen Anbieter einen API-Schlüssel hinterlegen.')
            self.settings.save(provider,self.model_value.get(),key,activate=True)
            self.refresh_settings(provider)
            self.settings_message.set('Aktive KI: '+PROVIDERS[provider]['name']+' / '+self.settings.model(provider))
        except Exception as e:
            messagebox.showerror('Einstellungen',str(e),parent=self)

    def test_provider(self):
        from core import AI
        provider=self.selected_provider()
        key=self.key_value.get().strip()
        model=self.model_value.get().strip()
        if PROVIDERS[provider]['env'] and not key:
            messagebox.showinfo('Verbindungstest','Zuerst einen API-Schlüssel eintragen.',parent=self)
            return
        if not model:
            messagebox.showinfo('Verbindungstest','Zuerst eine Modell-ID eintragen.',parent=self)
            return
        sources=[{'title':'Verbindungstest','document_id':0,'ordinal':1,'content':'Der Verbindungstest ist erfolgreich.'}]
        def run():
            AI(self.store,key,model,provider=provider).generate('Bestätige den Verbindungstest kurz mit Quellenverweis.',sources)
        def done(_):
            self.settings_message.set('Verbindung erfolgreich: '+PROVIDERS[provider]['name']+' / '+model+'. Die aktive KI bleibt bis zur Aktivierung unverändert.')
        self.work(run,done)

    def remove_provider_key(self):
        provider=self.selected_provider()
        if not PROVIDERS[provider]['env']:
            return
        if messagebox.askyesno('Schlüssel entfernen','Gespeicherten Schlüssel entfernen und diesen Anbieter für die Anwendung deaktivieren? Externe Schlüsseldateien bleiben erhalten.',parent=self):
            try:
                self.settings.remove_key(provider)
                self.refresh_settings(provider)
            except Exception as e:
                messagebox.showerror('Einstellungen',str(e),parent=self)

    def select_key(self):
        p=filedialog.askopenfilename(title='Vorhandene Schlüsseldatei auswählen',filetypes=[('Schlüsseldatei','*')])
        if not p:
            return
        try:
            count=self.settings.import_keys(p)
            self.refresh_settings()
            self.settings_message.set(f'{count} Anbieter-Schlüssel importiert. Aktive KI bleibt unverändert.')
        except Exception as e:
            self.refresh_settings()
            messagebox.showerror('Schlüsseldatei',str(e),parent=self)

    def delete(self):
        try:
            doc_id = self.selected()
            if messagebox.askyesno('Dokument löschen','Dokument und Suchabschnitte endgültig löschen? Frühere KI-Antworten behalten ihre Quellenkopien.',parent=self):
                self.store.delete(doc_id)
                self.refresh()
                self.write(self.detail,'Dokument gelöscht.')
        except Exception as e:
            messagebox.showerror('Löschen fehlgeschlagen',str(e),parent=self)

    def export(self):
        try:
            doc = self.store.get(self.selected())
            name = Path(doc['filename']).name if doc['filename'] else 'Notiz.txt'
            target = filedialog.asksaveasfilename(initialfile=name)
            if target:
                Path(target).write_bytes(doc['original'] if doc['original'] is not None else doc['content'].encode())
                self.status.set('Original exportiert')
        except Exception as e:
            messagebox.showerror('Export fehlgeschlagen',str(e),parent=self)

    def backup(self):
        target = filedialog.asksaveasfilename(defaultextension='.db',initialfile='wissen_sicherung.db')
        if target:
            self.work(lambda:self.store.backup(target),lambda _:self.status.set('SQL-Sicherung erstellt'))

    def show_health(self):
        def done(results):
            healthy = all(result.status == 'PASS' for result in results)
            show = messagebox.showinfo if healthy else messagebox.showwarning
            show('Systemstatus',format_diagnostics(results),parent=self)
        self.work(lambda:health_overview(self.store,self.settings,self.mail_accounts,PROVIDERS),done)

    def strip_old_mail_attachments(self):
        """Remove attachment content from IMAP originals stored before step 6.3a, after confirmation."""
        def confirm(result):
            if not result['changed']:
                messagebox.showinfo('Anhänge entfernen','Keine gespeicherte E-Mail enthält Anhangsinhalte. '
                                    'Es wurde nichts geändert.\n\n'+mail_cleanup.describe(result),parent=self)
                return
            postgres=self.store.backend=='postgresql'
            question=('Gefunden: '+mail_cleanup.describe(result)+'.\n\nDie Anhänge werden aus den gespeicherten '
                      'Originalen entfernt. Nachrichtentext und Suche bleiben unverändert. ')
            question+=('Hast du eine aktuelle pg_dump-Sicherung der PostgreSQL-Datenbank?' if postgres else
                       'Vorher wird eine geprüfte Sicherung neben der Datenbank abgelegt. Fortfahren?')
            if not messagebox.askyesno('Anhänge entfernen',question,parent=self):
                return
            self.work(lambda:mail_cleanup.strip_stored_attachments(self.store,backup_confirmed=postgres),
                      lambda done:messagebox.showinfo('Anhänge entfernt',mail_cleanup.describe(done),parent=self))
        self.work(lambda:mail_cleanup.strip_stored_attachments(self.store,dry_run=True),confirm)

    def restore(self):
        if self.store.backend != 'sqlite':
            messagebox.showinfo('Sicherung wiederherstellen',
                'Die Wiederherstellung in der Anwendung gilt nur für SQLite. '
                'PostgreSQL wird auf dem Server mit pg_restore wiederhergestellt.',parent=self)
            return
        source = filedialog.askopenfilename(parent=self,title='Sicherung auswählen',
            filetypes=[('SQLite-Sicherung','*.db *.bak *.sqlite'),('Alle Dateien','*.*')])
        if not source:
            return
        path = Path(self.store.path)
        confirmation = simpledialog.askstring('Sicherung wiederherstellen',
            'Der aktuelle Stand wird zuerst als geprüfte Sicherung abgelegt und dann '
            'vollständig durch die gewählte Sicherung ersetzt.\n\n'
            f'Zur Bestätigung den Dateinamen eingeben: {path.name}',parent=self)
        if confirmation is None:
            return
        if confirmation != path.name:
            messagebox.showerror('Sicherung wiederherstellen',
                'Dateiname stimmt nicht überein. Es wurde nichts geändert.',parent=self)
            return
        def run():
            result = restore_sqlite(path,source)
            migrate_sqlite(path)  # Older backups reach the current schema before use.
            return result
        def done(result):
            self.configuration_transfer.synchronize_authority()
            self.refresh()
            self.status.set(f"Sicherung wiederhergestellt: {result['documents']} Dokumente")
            messagebox.showinfo('Sicherung wiederherstellen',
                f"{result['documents']} Dokumente wiederhergestellt.\n"
                f"Vorheriger Stand: {result['safety_backup'] or 'keiner'}",parent=self)
        self.work(run,done)


def main():
    window=None
    try:
        window=App()
    except StartupFailure as error:
        show_startup_failure(error.results)
        return 1
    except Exception:
        print('Desktop-Oberfläche konnte nicht gestartet werden; Tcl/Tk und grafische Anzeige prüfen oder die CLI verwenden.',
              file=sys.stderr)
        return 1
    logger=create_operational_logger(
        BASE,'desktop',known_secrets=known_runtime_secrets(window.store))
    window.attach_operational_logger(logger)
    failed=False
    try:
        window.mainloop()
        return 0
    except Exception as error:
        failed=True
        window._safe_operational_event('application_failed',severity='ERROR',
            metadata={'backend':window.store.backend},exception_class=safe_exception_class(error),
            error_code='DESKTOP_RUNTIME_FAILED')
        raise
    finally:
        if failed:
            window.close_operational_logger()
        else:
            window.stop_operational_logging()


if __name__ == '__main__':
    raise SystemExit(main())
