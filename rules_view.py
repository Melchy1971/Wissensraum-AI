"""Settings page "Regeln": manage rules, apply them to existing documents, take runs back (step S2)."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import rules

SOURCE_CHOICES = [('', 'alle Quellen')] + list(rules.SOURCE_LABELS.items())
FIELDS = (('name', 'Name'), ('path_pattern', 'Ordner- oder Dateimuster'), ('sender_domain', 'Absenderdomäne'),
          ('extension', 'Dateiendung'), ('keyword', 'Stichwort'), ('category', 'Bereich setzen'),
          ('tags', 'Tags setzen (mit Komma getrennt)'))


def describe_conditions(rule):
    parts = []
    if rule['source_type']:
        parts.append('Quelle ' + rules.SOURCE_LABELS.get(rule['source_type'], rule['source_type']))
    if rule['path_pattern']:
        parts.append('Pfad ' + rule['path_pattern'])
    if rule['sender_domain']:
        parts.append('Absender @' + rule['sender_domain'])
    if rule['extension']:
        parts.append('Endung .' + rule['extension'])
    if rule['keyword']:
        parts.append('Stichwort „' + rule['keyword'] + '“')
    return ' und '.join(parts)


class RulesPage:
    def __init__(self, app, frame):
        self.app, self.frame = app, frame
        self.selected_id = None
        ttk.Label(frame, text='Regeln für Bereich und Tags', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, wraplength=900, text=(
            'Regeln wirken beim Import. Alle Bedingungen einer Regel müssen zutreffen. Den Bereich setzt die erste '
            'passende Regel von oben, Tags kommen von allen passenden Regeln. Notizen bleiben unverändert. '
            'Vorhandene Dokumente ändern sich erst nach Vorschau und Bestätigung.')).pack(anchor='w', pady=(6, 10))
        columns = ('position', 'enabled', 'name', 'conditions', 'category', 'tags')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', height=6, selectmode='browse')
        for column, title, width in (('position', 'Nr.', 40), ('enabled', 'Aktiv', 55), ('name', 'Name', 150),
                                     ('conditions', 'Bedingungen', 300), ('category', 'Bereich', 120),
                                     ('tags', 'Tags', 150)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width)
        self.tree.pack(fill='x')
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.load())
        form = ttk.Frame(frame, padding=(0, 12, 0, 6))
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.values = {name: tk.StringVar() for name, _ in FIELDS}
        self.enabled = tk.BooleanVar(value=True)
        self.source = tk.StringVar(value=SOURCE_CHOICES[0][1])
        row = 0
        for name, label in FIELDS[:1]:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=4)
            ttk.Entry(form, textvariable=self.values[name]).grid(row=row, column=1, sticky='ew', pady=4)
            row += 1
        ttk.Label(form, text='Quellentyp').grid(row=row, column=0, sticky='w', padx=(0, 20), pady=4)
        self.source_box = ttk.Combobox(form, textvariable=self.source, state='readonly',
                                       values=[label for _, label in SOURCE_CHOICES])
        self.source_box.grid(row=row, column=1, sticky='ew', pady=4)
        row += 1
        for name, label in FIELDS[1:]:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=4)
            ttk.Entry(form, textvariable=self.values[name]).grid(row=row, column=1, sticky='ew', pady=4)
            row += 1
        ttk.Checkbutton(form, text='Regel aktiv', variable=self.enabled).grid(row=row, column=1, sticky='w', pady=4)
        ttk.Label(form, wraplength=700, text='Muster ohne * oder ? treffen jeden Pfad, der den Text enthält, '
                  'z. B. „Rechnungen“. Mit * gilt das ganze Muster, z. B. „*/Rechnungen/*.pdf“. Die Absenderdomäne '
                  'umfasst auch ihre Unterdomänen.').grid(row=row+1, column=1, sticky='w')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(4, 6))
        self.action_buttons = {}
        for title, command in (('Neue Regel', self.new), ('Regel speichern', self.save), ('Regel löschen', self.delete),
                               ('Nach oben', lambda: self.move(-1)), ('Nach unten', lambda: self.move(1))):
            button = ttk.Button(actions, text=title, command=command)
            button.pack(side='left', padx=(0, 8))
            self.action_buttons[title] = button
        self.message = tk.StringVar()
        ttk.Label(frame, textvariable=self.message).pack(anchor='w', pady=(2, 10))
        ttk.Label(frame, text='Vorhandene Dokumente', font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=(8, 4))
        existing = ttk.Frame(frame)
        existing.pack(fill='x', pady=(0, 6))
        self.preview_button = ttk.Button(existing, text='Vorschau für vorhandene Dokumente', command=self.preview,
                                         style='Accent.TButton')
        self.preview_button.pack(side='left', padx=(0, 8))
        self.revert_button = ttk.Button(existing, text='Ausgewählten Lauf zurücknehmen', command=self.revert)
        self.revert_button.pack(side='left', padx=(0, 8))
        self.runs = ttk.Treeview(frame, columns=('id', 'created', 'documents', 'status'), show='headings', height=4,
                                 selectmode='browse')
        for column, title, width in (('id', 'Lauf', 60), ('created', 'Angewendet (UTC)', 170),
                                     ('documents', 'Dokumente', 90), ('status', 'Status', 220)):
            self.runs.heading(column, text=title)
            self.runs.column(column, width=width)
        self.runs.pack(fill='x')
        self.refresh()

    # --- rules ---------------------------------------------------------------------------------
    def refresh(self, select=None):
        with self.app.store.connect() as db:
            rows, run_rows = rules.list_rules(db), rules.runs(db)
        self.tree.delete(*self.tree.get_children())
        for number, rule in enumerate(rows, start=1):
            self.tree.insert('', 'end', iid=str(rule['id']), values=(
                number, 'JA' if rule['enabled'] else 'NEIN', rule['name'], describe_conditions(rule),
                rule['category'] or '–', rule['tags'] or '–'))
        if select is not None and self.tree.exists(str(select)):
            self.tree.selection_set(str(select))
        self.runs.delete(*self.runs.get_children())
        for run in run_rows:
            status = 'angewendet' if run['status'] == 'applied' else f'zurückgenommen {run["reverted_at"]}'
            self.runs.insert('', 'end', iid=str(run['id']), values=(run['id'], run['created_at'], run['documents'],
                                                                    status))

    def form(self):
        data = {name: variable.get() for name, variable in self.values.items()}
        labels = {label: key for key, label in SOURCE_CHOICES}
        data['source_type'] = labels.get(self.source.get(), '')
        data['enabled'] = self.enabled.get()
        return data

    def load(self):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        with self.app.store.connect() as db:
            rule = next(rule for rule in rules.list_rules(db) if rule['id'] == self.selected_id)
        for name, variable in self.values.items():
            variable.set(rule[name])
        self.source.set(dict(SOURCE_CHOICES).get(rule['source_type'], SOURCE_CHOICES[0][1]))
        self.enabled.set(bool(rule['enabled']))
        self.message.set('')

    def new(self):
        self.selected_id = None
        self.tree.selection_remove(*self.tree.selection())
        for variable in self.values.values():
            variable.set('')
        self.source.set(SOURCE_CHOICES[0][1])
        self.enabled.set(True)
        self.message.set('Neue Regel: Felder ausfüllen und speichern.')

    def save(self):
        try:
            with self.app.store.connect() as db:
                self.selected_id = rules.save_rule(db, self.form(), self.selected_id)
        except ValueError as error:
            messagebox.showerror('Regel nicht gespeichert', str(error), parent=self.app)
            return
        self.refresh(self.selected_id)
        self.message.set('Regel gespeichert. Sie wirkt ab dem nächsten Import.')

    def delete(self):
        if self.selected_id is None:
            messagebox.showerror('Regel löschen', 'Zuerst eine Regel auswählen.', parent=self.app)
            return
        if not messagebox.askyesno('Regel löschen', 'Regel löschen? Bereits gesetzte Bereiche und Tags bleiben '
                                   'erhalten.', parent=self.app):
            return
        with self.app.store.connect() as db:
            rules.delete_rule(db, self.selected_id)
        self.new()
        self.refresh()
        self.message.set('Regel gelöscht.')

    def move(self, offset):
        if self.selected_id is None:
            return
        with self.app.store.connect() as db:
            rules.move_rule(db, self.selected_id, offset)
        self.refresh(self.selected_id)

    # --- existing documents ----------------------------------------------------------------------
    def preview(self):
        def done(rows):
            if not rows:
                messagebox.showinfo('Vorschau', 'Die aktiven Regeln ändern kein vorhandenes Dokument.', parent=self.app)
                return
            self.show_preview(rows)

        def run():
            with self.app.store.connect() as db:
                return rules.preview(db)
        self.app.work(run, done)

    def show_preview(self, rows):
        window = tk.Toplevel(self.app)
        window.title('Regeln auf vorhandene Dokumente anwenden')
        window.geometry('900x520')
        window.transient(self.app)
        self.preview_window = window
        bottom = ttk.Frame(window, padding=12)
        bottom.pack(side='bottom', fill='x')
        ttk.Label(bottom, text=f'{len(rows)} Dokumente würden geändert. Noch ist nichts gespeichert. '
                  'Der Lauf lässt sich später vollständig zurücknehmen.', wraplength=860).pack(anchor='w')
        buttons = ttk.Frame(bottom)
        buttons.pack(fill='x', pady=(8, 0))
        tree = ttk.Treeview(window, columns=('title', 'old', 'new', 'tags'), show='headings')
        for column, title, width in (('title', 'Titel', 280), ('old', 'Bereich bisher', 170),
                                     ('new', 'Bereich neu', 170), ('tags', 'Neue Tags', 200)):
            tree.heading(column, text=title)
            tree.column(column, width=width)
        for index, row in enumerate(rows):
            tree.insert('', 'end', iid=str(index), values=(
                row['title'], row['category'],
                row['new_category'] if row['new_category'] != row['category'] else 'unverändert',
                ', '.join(row['new_tags']) or '–'))
        tree.pack(fill='both', expand=True, padx=12, pady=(12, 0))
        window.preview_tree = tree

        def confirm():
            window.destroy()
            self.apply(rows)
        window.confirm_button = ttk.Button(buttons, text='Änderungen übernehmen', command=confirm,
                                           style='Accent.TButton')
        window.confirm_button.pack(side='left', padx=(0, 8))
        window.cancel_button = ttk.Button(buttons, text='Abbrechen', command=window.destroy)
        window.cancel_button.pack(side='left')
        return window

    def apply(self, rows):
        def run():
            with self.app.store.connect() as db:
                return rules.apply(db, self.app.store.backend, rows)

        def done(result):
            self.refresh()
            self.app.refresh()
            skipped = f' {result["skipped"]} zwischenzeitlich geänderte Dokumente wurden übersprungen.' \
                if result['skipped'] else ''
            self.message.set(f'{result["changed"]} Dokumente geändert.{skipped}')
        self.app.work(run, done)

    def revert(self):
        selection = self.runs.selection()
        if not selection:
            messagebox.showerror('Lauf zurücknehmen', 'Zuerst einen Lauf auswählen.', parent=self.app)
            return
        run_id = int(selection[0])
        if not messagebox.askyesno('Lauf zurücknehmen', f'Lauf {run_id} vollständig zurücknehmen? Bereiche und '
                                   'Tags werden auf den Stand vor dem Lauf gesetzt.', parent=self.app):
            return

        def run():
            with self.app.store.connect() as db:
                return rules.revert(db, self.app.store.backend, run_id)

        def done(result):
            self.refresh()
            self.app.refresh()
            conflicts = (f' {result["conflicts"]} Dokumente behalten ihren später geänderten Bereich.'
                         if result['conflicts'] else '')
            self.message.set(f'Lauf {run_id} zurückgenommen: {result["restored"]} Dokumente.{conflicts}')
        self.app.work(run, done)
