"""Semantic search controls on the "KI-Anbieter" settings page and the embedding timer (step 7.2b)."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

import vector_sync

TICK_MS = 30_000
STARTUP_DELAY_MS = 10_000


def state_text(state, progress):
    done, total = progress
    if not state['enabled']:
        return f'Semantische Suche ist aus. {done} von {total} Abschnitten haben einen Vektor. ' + state['last_result']
    return f'Aktiv mit {state["model"]}. {done} von {total} Abschnitten eingebettet. {state["last_result"]}'.strip()


class VectorPanel:
    def __init__(self, app, frame):
        self.app = app
        self.sync = vector_sync.VectorSync(app.store)
        self.running = False
        self.pending = None  # result of a timer run, shown by ``poll`` in the Tk thread
        ttk.Label(frame, text='Semantische Suche (Vorbereitung)', font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=(16, 2))
        ttk.Label(frame, wraplength=950, text=(
            'Berechnet für jeden Textabschnitt einen Vektor mit dem lokalen Ollama-Modell. Die Texte verlassen '
            'diesen Rechner nicht. Läuft nur, solange die Anwendung geöffnet ist, in kleinen Schritten im Hintergrund; '
            'neue Importe werden automatisch erfasst. Die Suche nutzt die Vektoren erst in einem späteren Schritt.')
        ).pack(anchor='w', pady=(0, 6))
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Label(row, text='Ollama-Modell').pack(side='left')
        self.model = tk.StringVar(value=self.sync.config()['model'])
        ttk.Entry(row, textvariable=self.model, width=28).pack(side='left', padx=(6, 16))
        self.activate_button = ttk.Button(row, text='Aktivieren', command=self.activate, style='Accent.TButton')
        self.activate_button.pack(side='left', padx=(0, 8))
        self.deactivate_button = ttk.Button(row, text='Deaktivieren', command=self.deactivate)
        self.deactivate_button.pack(side='left')
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, wraplength=950).pack(anchor='w', pady=(4, 0))
        self.load()
        app.after(STARTUP_DELAY_MS, self.tick)
        app.after(500, self.poll)

    def load(self):
        try:
            self.status.set(state_text(self.sync.config(), self.sync.progress()))
        except Exception as error:  # an unreadable store must not break the settings page
            self.status.set('Status nicht lesbar: ' + str(error))

    def activate(self):
        model = self.model.get().strip()
        self.app.work(lambda: self.sync.activate(model), lambda _: self.load())

    def deactivate(self):
        self.sync.deactivate()
        self.load()

    # --- timer of the open application -------------------------------------------------------
    def tick(self):
        """Start one bounded run in a worker thread; never while another run of the timer is active."""
        if not self.running:
            try:
                due = self.sync.due()
            except Exception:  # e.g. database busy: try at the next tick
                due = False
            if due:
                self.running = True

                def worker():
                    try:
                        self.pending = self.sync.run()
                    except Exception as error:
                        self.pending = {'embedded': 0, 'last_result': str(error)}
                    finally:
                        self.running = False
                threading.Thread(target=worker, daemon=True).start()
        self.app.after(TICK_MS, self.tick)

    def poll(self):
        result, self.pending = self.pending, None
        if result:
            self.load()
            if result.get('embedded'):
                self.app.status.set(f'Semantische Suche: {result["embedded"]} Abschnitte eingebettet')
        self.app.after(500, self.poll)
