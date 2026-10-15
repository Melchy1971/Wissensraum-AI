"""Native desktop interface. Run: python app.py"""
from pathlib import Path
import os
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from core import Store, AI, import_document, load_key

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
        self.key_override = None
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
        for label, command in [('Dateien importieren',self.import_files),('Notiz erfassen',self.note),
                               ('Datenbank sichern',self.backup),('API-Schlüsseldatei',self.select_key)]:
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
        ttk.Label(self.chat,text='Passende Textstellen und deine Frage werden bei einer KI-Anfrage an OpenAI gesendet.').pack(anchor='w',pady=(4,10))
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
        return AI(self.store,self.key_override or load_key(BASE),os.environ.get('OPENAI_MODEL','gpt-4.1-mini'))

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

    def select_key(self):
        p = filedialog.askopenfilename(title='Vorhandene .env.local mit OPENAI_API_KEY auswählen',filetypes=[('Schlüsseldatei','*')])
        if not p:
            return
        try:
            lines = Path(p).read_text(encoding='utf-8').splitlines()
            value = next((line.partition('=')[2].strip().strip('\"\'') for line in lines if line.partition('=')[0].strip()=='OPENAI_API_KEY'),'')
            if not value.startswith('sk-'):
                raise ValueError('Datei enthält keinen gültig formatierten OPENAI_API_KEY.')
            self.key_override = value
            self.status.set('API-Schlüssel für diese Sitzung geladen')
        except Exception as e:
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
