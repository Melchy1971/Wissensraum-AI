"""Native desktop interface. Run: python app.py"""
from pathlib import Path
import os
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from core import Store, import_document
from settings import Settings, PROVIDERS
from importer import Importer, MailAccounts, MAIL_PROVIDERS

BASE = Path(__file__).resolve().parent


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Wissensraum · SQL & KI')
        self.geometry('1180x780')
        self.minsize(920, 650)
        self.configure(bg='#f3f4f0')
        self.store = Store(Path(os.environ.get('WISSEN_DB', BASE/'data'/'wissen.db')))
        self.queue = queue.Queue()
        self.busy = False
        self.settings = Settings(self.store, BASE)
        self.mail_accounts = MailAccounts(self.store, self.settings.vault)
        self.importer = Importer(self.store, self.mail_accounts)
        self.docs = {}
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.style.configure('.', font=('Segoe UI', 11), background='#f3f4f0', foreground='#182c2a')
        self.style.configure('TButton', padding=(12,8))
        self.style.configure('Accent.TButton', background='#185b50', foreground='white')
        self.style.map('Accent.TButton', background=[('active','#12483e'),('disabled','#65736e')])
        self.style.configure('Treeview', rowheight=32, fieldbackground='white', background='white')
        self.style.map('Treeview', background=[('selected','#185b50')], foreground=[('selected','white')])
        header = ttk.Frame(self, padding=(24,18))
        header.pack(fill='x')
        ttk.Label(header, text='Wissensraum', font=('Segoe UI',24,'bold')).pack(side='left')
        ttk.Label(header, text='Dokumente verstehen. Wissen wiederfinden.').pack(side='left', padx=24)
        tools = ttk.Frame(self, padding=(24,0,24,12))
        tools.pack(fill='x')
        self.buttons = []
        for label, command in [('Import',self.open_import),('Notiz erfassen',self.note),
                               ('Datenbank sichern',self.backup),('Einstellungen',self.open_settings)]:
            b = ttk.Button(tools,text=label,command=command)
            b.pack(side='left',padx=(0,8))
            self.buttons.append(b)
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
        split = ttk.Panedwindow(self.library,orient='horizontal')
        split.pack(fill='both',expand=True)
        left, right = ttk.Frame(split), ttk.Frame(split)
        split.add(left,weight=2)
        split.add(right,weight=3)
        self.tree = ttk.Treeview(left,columns=('title','category'),show='headings',selectmode='browse')
        self.tree.heading('title',text='Titel')
        self.tree.heading('category',text='Bereich')
        self.tree.column('title',width=230)
        self.tree.column('category',width=110)
        scroll = ttk.Scrollbar(left,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.show_document())
        row = ttk.Frame(right)
        row.pack(fill='x',padx=(14,0),pady=(0,8))
        for label,command in [('Zusammenfassen',self.summarize),('Original exportieren',self.export),('Löschen',self.delete)]:
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
        self.answer = ScrolledText(self.chat,wrap='word',font=('Segoe UI',12),padx=18,pady=16,
                                   background='white',foreground='#182c2a')
        self.answer.pack(fill='both',expand=True,pady=(14,0))
        self.write(self.answer,'Stelle eine Frage mit Begriffen aus deinen Dokumenten.\n\nDie Suche arbeitet mit Volltext. Antworten zeigen Quellen als [Q1], [Q2] usw. Prüfe die zugrunde liegenden Textstellen. KI-Aussagen können fehlerhaft sein.')
        ttk.Button(self.history_tab,text='Verlauf aktualisieren',command=self.show_history).pack(anchor='w',pady=(0,12))
        self.history = ScrolledText(self.history_tab,wrap='word',font=('Segoe UI',11),padx=14,pady=12)
        self.history.pack(fill='both',expand=True)
        self.status = tk.StringVar(value='Bereit · SQLite · Einzelplatz')
        ttk.Label(self,textvariable=self.status,padding=(24,0,24,12)).pack(fill='x')
        self.build_import()
        self.build_settings()
        self.refresh()
        self.show_history()
        self.after(100,self.poll)

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
            if query:
                found = self.store.search(query,30)
                ids = list(dict.fromkeys(r['document_id'] for r in found))
                rows = [self.store.get(i) for i in ids]
            else:
                rows = self.store.list()
            self.tree.delete(*self.tree.get_children())
            for r in rows:
                self.tree.insert('', 'end', iid=str(r['id']),values=(r['title'],r['category']))
            suffix = ' · maximal 30 Texttreffer' if query else ''
            self.status.set(f'{len(rows)} Dokumente angezeigt{suffix}')
        except Exception as e:
            messagebox.showerror('Suche fehlgeschlagen',str(e),parent=self)

    def show_document(self):
        if self.tree.selection():
            doc = self.store.get(self.selected())
            self.write(self.detail,f'{doc["title"]}\nBereich: {doc["category"]}\nErfasst: {doc["created_at"]} UTC\n\n{doc["content"]}')

    def work(self, fn, done):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.configure(state='disabled')
        self.status.set('Wird verarbeitet …')
        def worker():
            try:
                self.queue.put((done,fn(),None))
            except Exception as e:
                self.queue.put((done,None,str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            done,result,error = self.queue.get_nowait()
            self.busy = False
            for button in self.buttons:
                button.configure(state='normal')
            if error:
                self.status.set('Aktion fehlgeschlagen')
                messagebox.showerror('Aktion fehlgeschlagen',error,parent=self)
            else:
                self.status.set('Erledigt')
                done(result)
        except queue.Empty:
            pass
        self.after(100,self.poll)

    def import_files(self):
        paths = filedialog.askopenfilenames(filetypes=[('Dokumente','*.txt *.md *.pdf *.docx')])
        if not paths:
            return
        def run():
            report = []
            for path in paths:
                try:
                    title,text,name,data = import_document(path)
                    _,created = self.store.add(title,text,filename=name,original=data)
                    report.append(f'{name}: '+('importiert' if created else 'bereits vorhanden'))
                except Exception as e:
                    report.append(f'{Path(path).name}: FEHLER – {e}')
            return '\n'.join(report)
        def done(report):
            self.refresh()
            messagebox.showinfo('Importergebnis',report,parent=self)
        self.work(run,done)

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
        answer,sources = result
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
        for item in items:
            sources = json.loads(item['sources_json'])
            refs = '\n'.join(f'[Q{i+1}] {s["title"]} · Dokument {s["document_id"]} · Abschnitt {s["ordinal"]}\n{s["content"]}' for i,s in enumerate(sources))
            text.append(f'{item["created_at"]} UTC · {item["model"]}\n{item["question"]}\n\n{item["answer"]}\n\nQuellen zum Anfragezeitpunkt:\n{refs}')
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
        ttk.Label(frame,text='Importierte Inhalte werden als durchsuchbare Dokumente in SQLite gespeichert.',wraplength=900).pack(anchor='w',pady=(5,14))
        local=ttk.LabelFrame(frame,text='Lokale Dokumente',padding=14)
        local.pack(fill='x',pady=(0,10))
        ttk.Label(local,text='TXT, Markdown, PDF mit Text und DOCX · maximal 20 MB je Datei').pack(side='left')
        b=ttk.Button(local,text='Dokumente auswählen',command=self.import_files,style='Accent.TButton')
        b.pack(side='right'); self.buttons.append(b)
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
        return f'{report["found"]} gefunden · {report["imported"]} importiert · {report["duplicates"]} bereits vorhanden · {report["errors"]} Fehler'

    def import_pst_file(self):
        path=filedialog.askopenfilename(title='Outlook-PST-Datei auswählen',filetypes=[('Outlook-PST','*.pst')])
        if not path: return
        try: limit=int(self.import_limit.get())
        except ValueError:
            messagebox.showerror('PST-Import','Gültige maximale Anzahl eingeben.',parent=self); return
        self.work(lambda:self.importer.import_pst(path,limit),lambda result:(self.refresh(),self.import_message.set('PST: '+self.report_text(result))))

    def import_mailbox(self):
        account_id=self.import_account_ids.get(self.import_account_value.get())
        if not account_id:
            messagebox.showinfo('E-Mail-Import','Zuerst unter Einstellungen ein E-Mail-Konto anlegen.',parent=self); return
        try: limit=int(self.import_limit.get())
        except ValueError:
            messagebox.showerror('E-Mail-Import','Gültige maximale Anzahl eingeben.',parent=self); return
        folder=self.import_folder.get().strip() or 'INBOX'
        self.work(lambda:self.importer.import_imap(account_id,folder,limit),
                  lambda result:(self.refresh(),self.import_message.set('Postfach: '+self.report_text(result))))

    def open_mail_settings(self):
        self.tabs.select(self.settings_tab)
        self.settings_sections.select(self.mail_settings_frame)
        self.refresh_mail_accounts()

    def build_settings(self):
        self.settings_sections=ttk.Notebook(self.settings_tab)
        self.settings_sections.pack(fill='both',expand=True)
        self.ai_settings_frame=ttk.Frame(self.settings_sections,padding=12)
        self.mail_settings_frame=ttk.Frame(self.settings_sections,padding=12)
        self.settings_sections.add(self.ai_settings_frame,text='KI-Anbieter')
        self.settings_sections.add(self.mail_settings_frame,text='E-Mail-Konten')
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
        frame.bind('<Configure>',self.resize_settings_text,add='+')
        self.refresh_settings()
        self.build_mail_settings()

    def build_mail_settings(self):
        frame=self.mail_settings_frame
        ttk.Label(frame,text='E-Mail-Konten',font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Kontodaten werden in SQLite gespeichert; Kennwörter und OAuth-Token im Betriebssystem-Schlüsselspeicher.',wraplength=950).pack(anchor='w',pady=(5,10))
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

    def load_mail_account(self):
        selected=self.mail_tree.selection()
        if not selected: return
        row=self.mail_rows[selected[0]]
        for name in ('id','label','provider','email','username','host','port','client_id','tenant'):
            self.mail_vars[name].set(row[name])
        self.mail_vars['secret'].set(''); self.apply_mail_provider()
        self.mail_message.set(row['auth_status']+' · Bestehende Zugangsdaten bleiben verborgen.')

    def apply_mail_provider(self):
        provider=self.mail_vars['provider'].get() or 'gmail'
        self.mail_provider_value.set(MAIL_PROVIDERS[provider]['name'])
        if provider=='gmail':
            self.mail_vars['host'].set('imap.gmail.com'); self.mail_vars['port'].set('993')
        elif provider=='microsoft':
            self.mail_vars['host'].set(''); self.mail_vars['port'].set('0'); self.mail_vars['tenant'].set(self.mail_vars['tenant'].get() or 'common')
        for name in ('host','port','username','secret'):
            self.mail_fields.get(name,self.mail_secret).configure(state='disabled' if provider=='microsoft' else 'normal')
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

    def resize_settings_text(self,event):
        """Keep explanatory text readable without wasting vertical space."""
        width=max(420,event.width-20)
        self.settings_status.configure(wraplength=width)
        self.settings_help.configure(wraplength=width)
        self.key_hint.configure(wraplength=max(360,width-195))

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


if __name__ == '__main__':
    App().mainloop()
