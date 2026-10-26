"""Mailbox sync controls on the "E-Mail-Konten" settings page and the timer of the open application (step S3)."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import mail_sync

TICK_MS = 60_000
INTERVAL_LABELS = {mail_sync.interval_label(minutes): minutes for minutes in mail_sync.INTERVALS}


def state_text(state):
    if not state['enabled']:
        return 'Automatische Synchronisation ist aus. ' + (state['last_result'] or '')
    if state['stopped']:
        return 'Gestoppt: ' + state['stopped'] + ' Nach der Korrektur erneut aktivieren.'
    last = time.strftime('%d.%m.%Y %H:%M', time.localtime(state['last_run'])) if state['last_run'] else '–'
    return (f'Aktiv, alle {mail_sync.interval_label(state["interval"])}, Ordner {state["folder"]}. '
            f'Letzter Abruf {last}: {state["last_result"]}')


class SyncPanel:
    def __init__(self, app, frame):
        self.app, self.frame = app, frame
        self.sync = mail_sync.MailSync(app.store, app.importer, app.import_jobs)
        self.running = False
        self.pending = None  # result of a timer run, shown by ``poll`` in the Tk thread
        ttk.Label(frame, text='Automatische Synchronisation', font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=(16, 2))
        ttk.Label(frame, wraplength=950, text=(
            'Nur solange die Anwendung geöffnet ist. Abgerufen werden nur Nachrichten, die nach der Aktivierung '
            'eingehen, als normaler Importauftrag. Anhänge werden nie automatisch geholt, sondern weiter einzeln '
            'über die Vorschau freigegeben. Bei einem Anmeldefehler stoppt die Synchronisation mit Grund.')
        ).pack(anchor='w', pady=(0, 6))
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Label(row, text='Intervall').pack(side='left')
        self.interval = tk.StringVar(value=mail_sync.interval_label(mail_sync.DEFAULT_INTERVAL))
        self.interval_box = ttk.Combobox(row, textvariable=self.interval, state='readonly', width=10,
                                         values=list(INTERVAL_LABELS))
        self.interval_box.pack(side='left', padx=(6, 16))
        ttk.Label(row, text='Ordner (IMAP)').pack(side='left')
        self.folder = tk.StringVar(value='INBOX')
        self.folder_entry = ttk.Entry(row, textvariable=self.folder, width=24)
        self.folder_entry.pack(side='left', padx=(6, 0))
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(8, 4))
        self.activate_button = ttk.Button(actions, text='Synchronisation aktivieren', command=self.activate,
                                          style='Accent.TButton')
        self.activate_button.pack(side='left', padx=(0, 8))
        self.deactivate_button = ttk.Button(actions, text='Deaktivieren', command=self.deactivate)
        self.deactivate_button.pack(side='left', padx=(0, 8))
        self.run_button = ttk.Button(actions, text='Jetzt abrufen', command=self.run_now)
        self.run_button.pack(side='left', padx=(0, 8))
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, wraplength=950).pack(anchor='w', pady=(4, 0))
        app.after(TICK_MS, self.tick)
        app.after(200, self.poll)

    def account_id(self):
        account_id = self.app.mail_vars['id'].get()
        if not account_id:
            raise ValueError('Zuerst ein gespeichertes Konto auswählen.')
        return account_id

    def load(self):
        account_id = self.app.mail_vars['id'].get()
        if not account_id:
            self.status.set('Konto zuerst speichern, dann lässt sich die Synchronisation aktivieren.')
            return
        state = self.sync.config(account_id)
        self.interval.set(mail_sync.interval_label(state['interval']))
        self.folder.set(state['folder'])
        self.folder_entry.configure(state='disabled' if self.app.mail_vars['provider'].get() == 'microsoft' else 'normal')
        self.status.set(state_text(state))

    def activate(self):
        try:
            account_id = self.account_id()
        except ValueError as error:
            messagebox.showerror('Synchronisation', str(error), parent=self.app)
            return
        interval, folder = INTERVAL_LABELS[self.interval.get()], self.folder.get()
        self.app.work(lambda: self.sync.activate(account_id, interval, folder), lambda _: self.load())

    def deactivate(self):
        try:
            self.sync.deactivate(self.account_id())
        except ValueError as error:
            messagebox.showerror('Synchronisation', str(error), parent=self.app)
            return
        self.load()

    def run_now(self):
        try:
            account_id = self.account_id()
        except ValueError as error:
            messagebox.showerror('Synchronisation', str(error), parent=self.app)
            return

        def done(state):
            self.load()
            if state['imported']:
                self.app.refresh()
        self.app.work(lambda: self.sync.run(account_id), done)

    # --- timer of the open application -------------------------------------------------------
    def tick(self):
        """Start due runs in a worker thread; never while another run of the timer is active."""
        if not self.running and self.sync.due():
            self.running = True

            def worker():
                try:
                    self.pending = self.sync.run_due()
                except Exception as error:  # reported in the status line, the timer keeps going
                    self.pending = {'': {'imported': 0, 'last_result': str(error)}}
                finally:
                    self.running = False
            threading.Thread(target=worker, daemon=True).start()
        self.app.after(TICK_MS, self.tick)

    def poll(self):
        results, self.pending = self.pending, None
        if results:
            imported = sum(state.get('imported', 0) for state in results.values())
            stopped = [state['stopped'] for state in results.values() if state.get('stopped')]
            message = f'Synchronisation: {imported} neue E-Mails'
            if stopped:
                message += ' · gestoppt: ' + stopped[0]
            if imported:
                self.app.refresh()
            self.app.status.set(message)
            self.load()
        self.app.after(500, self.poll)
