"""Graph view with filters (Release 2.3, step 8B.4): entities as nodes, relations as edges.

The layout lives in ``graph_layout`` without Tk, so that the web page (phase 7) uses the same one.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from graph_layout import TYPE_COLOURS, layout  # noqa: E402,F401  (layout also imported from here)


class GraphWindow(tk.Toplevel):
    """Filters on the left, canvas on the right; a click on a node shows its evidence."""

    def __init__(self, app, store):
        super().__init__(app)
        self.app, self.store = app, store
        self.title('Graphansicht')
        self.geometry('1180x760')
        self.transient(app)
        side = ttk.Frame(self, padding=(16, 16, 8, 16))
        side.pack(side='left', fill='y')
        ttk.Label(side, text='Graphansicht', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(side, text='Entitätsarten').pack(anchor='w', pady=(12, 2))
        self.entity_types = tk.Listbox(side, selectmode='multiple', exportselection=False, height=7)
        self.entity_types.pack(fill='x')
        ttk.Label(side, text='Relationsarten').pack(anchor='w', pady=(10, 2))
        self.relation_types = tk.Listbox(side, selectmode='multiple', exportselection=False, height=7)
        self.relation_types.pack(fill='x')
        self.include_proposed = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text='Ungeprüfte Vorschläge zeigen', variable=self.include_proposed).pack(anchor='w', pady=(10, 0))
        ttk.Label(side, text='Name enthält').pack(anchor='w', pady=(10, 2))
        self.query = tk.StringVar()
        ttk.Entry(side, textvariable=self.query, width=28).pack(fill='x')
        ttk.Label(side, text='Umfeld der gewählten Entität (Schritte)').pack(anchor='w', pady=(10, 2))
        self.depth = tk.IntVar(value=0)
        ttk.Spinbox(side, from_=0, to=3, textvariable=self.depth, width=5).pack(anchor='w')
        ttk.Button(side, text='Anzeigen', command=self.refresh, style='Accent.TButton').pack(fill='x', pady=(12, 4))
        ttk.Button(side, text='Filter zurücksetzen', command=self.reset).pack(fill='x')
        self.info = tk.StringVar()
        ttk.Label(side, textvariable=self.info, wraplength=260).pack(anchor='w', pady=(10, 4))
        self.detail = tk.Text(side, width=36, height=12, wrap='word', state='disabled')
        self.detail.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(self, background='white', highlightthickness=0)
        self.canvas.pack(side='left', fill='both', expand=True, padx=(0, 16), pady=16)
        self.focus, self.items, self.data = None, {}, {'nodes': [], 'edges': []}
        self.type_ids, self.relation_ids = [], []
        self.load_types()
        self.update_idletasks()
        self.refresh()

    def load_types(self):
        self.type_ids = [row['id'] for row in self.store.entity_types()]
        self.relation_ids = [row['id'] for row in self.store.relation_types()]
        self.colours = {type_id: TYPE_COLOURS[index % len(TYPE_COLOURS)] for index, type_id in enumerate(self.type_ids)}
        for box, rows in ((self.entity_types, self.store.entity_types()), (self.relation_types, self.store.relation_types())):
            box.delete(0, 'end')
            for row in rows:
                box.insert('end', row['name'])

    def reset(self):
        for box in (self.entity_types, self.relation_types):
            box.selection_clear(0, 'end')
        self.include_proposed.set(False); self.query.set(''); self.depth.set(0); self.focus = None
        self.refresh()

    def filters(self):
        types = [self.type_ids[i] for i in self.entity_types.curselection()] or None
        relations = [self.relation_ids[i] for i in self.relation_types.curselection()] or None
        depth = int(self.depth.get() or 0)
        return dict(entity_types=types, relation_types=relations, include_proposed=self.include_proposed.get(),
                    query=self.query.get().strip() or None, focus=self.focus if depth else None, depth=depth or 1)

    def refresh(self):
        try:
            self.data = self.store.graph_data(**self.filters())
        except ValueError as error:
            messagebox.showerror('Graphansicht', str(error), parent=self)
            self.focus = None
            return
        self.draw()
        nodes, edges = len(self.data['nodes']), len(self.data['edges'])
        self.info.set(f'{nodes} Entitäten, {edges} Relationen' + (' (gekürzt auf die am stärksten verbundenen)'
                                                                   if self.data['truncated'] else ''))

    def draw(self):
        canvas = self.canvas
        canvas.delete('all')
        self.items = {}
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 300)
        positions = layout(self.data['nodes'], self.data['edges'], width, height, self.focus)
        for edge in self.data['edges']:
            (x1, y1), (x2, y2) = positions[edge['source_id']], positions[edge['target_id']]
            dash = (4, 3) if edge['status'] == 'proposed' else None
            canvas.create_line(x1, y1, x2, y2, fill='#888888', arrow='last', dash=dash, width=1.5)
            canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=edge['type'], fill='#555555', font=('Segoe UI', 8))
        for node in self.data['nodes']:
            x, y = positions[node['id']]
            radius = 9 + min(node['degree'], 8)
            outline = '#000000' if node['id'] == self.focus else ''
            oval = canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=self.colours.get(node['type_id'], '#6b6b6b'),
                                      outline=outline, width=2, dash=(3, 2) if node['status'] == 'proposed' else None)
            label = canvas.create_text(x, y + radius + 9, text=node['name'], font=('Segoe UI', 9))
            for item in (oval, label):
                self.items[item] = node['id']
                canvas.tag_bind(item, '<Button-1>', lambda _, n=node['id']: self.select(n))
                canvas.tag_bind(item, '<Double-Button-1>', lambda _, n=node['id']: self.center(n))

    def select(self, entity_id):
        entity = self.store.entity(entity_id)
        lines = [f'{entity["name"]} ({entity["type"]}, {"ungeprüft" if entity["status"] == "proposed" else "bestätigt"})', '']
        lines += [f'„{m["quote"]}“ in {m["document"]} (#{m["document_id"]})' for m in entity['mentions']]
        lines += [''] + [f'{r["source"]} {r["type"]} {r["target"]}: „{r["quote"]}“' for r in entity['relations']]
        self.detail.configure(state='normal'); self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', '\n'.join(lines)); self.detail.configure(state='disabled')
        self.focus = entity_id

    def center(self, entity_id):
        """Double click: the entity's surroundings (at least one step)."""
        self.focus = entity_id
        if not int(self.depth.get() or 0):
            self.depth.set(1)
        self.refresh()
