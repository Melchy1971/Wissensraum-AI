"""Native UI regression; skipped on systems without a graphical display."""
from pathlib import Path
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from settings import Settings
from test_settings import FakeVault

class InterfaceTests(unittest.TestCase):
    def setUp(self):
        try:
            probe=tk.Tk()
            probe.destroy()
        except tk.TclError:
            self.skipTest('Keine grafische Anzeige verfügbar')
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.base.joinpath('.env.local').write_text('OPENAI_API_KEY=synthetic-existing-1234567890',encoding='utf-8')
        self.vault=FakeVault()
        self.patches=[patch.object(app,'BASE',self.base),patch.dict(os.environ,{k:os.environ[k] for k in ('DISPLAY','XAUTHORITY','PATH','SYSTEMROOT','SystemRoot','WINDIR','HOME','USERPROFILE','TCL_LIBRARY','TK_LIBRARY') if k in os.environ},clear=True),
                      patch.object(app,'Settings',lambda store,base:Settings(store,base,self.vault,environ={}))]
        for p in self.patches: p.start()
        self.window=None
        self.addCleanup(self.cleanup)
        self.window=app.App()
        self.window.update()

    def cleanup(self):
        if self.window is not None:
            self.window.destroy()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def test_existing_keys_are_visible_but_masked(self):
        window=self.window
        self.assertIn('Einstellungen',[window.tabs.tab(t,'text') for t in window.tabs.tabs()])
        self.assertEqual(window.key_value.get(),'synthetic-existing-1234567890')
        self.assertEqual(window.key_entry.cget('show'),'*')
        values=window.provider_tree.item('openai','values')
        self.assertEqual(values[0],'AKTIV')
        self.assertIn('7890',values[3])
        self.assertNotIn('synthetic-existing',values[3])
        window.open_settings()
        self.assertEqual(window.tabs.select(),str(window.settings_tab))

    def test_save_switch_reload_and_remove(self):
        window=self.window
        window.provider_tree.selection_set('anthropic')
        window.update()
        window.key_value.set('synthetic-claude-abcdefgh')
        window.save_provider()
        self.assertEqual(window.settings.active(),'openai')
        window.activate_provider()
        self.assertEqual(window.settings.active(),'anthropic')
        self.assertEqual(window.provider_tree.item('anthropic','values')[0],'AKTIV')
        self.assertEqual(window.provider_tree.item('openai','values')[0],'')
        self.assertIn('Claude',window.provider_notice.get())
        self.assertEqual(window.settings.ai().provider,'anthropic')
        with patch('app.messagebox.askyesno',return_value=True):
            window.remove_provider_key()
        self.assertEqual(window.key_value.get(),'')
        window.provider_tree.selection_set('ollama')
        window.update()
        window.activate_provider()
        self.assertEqual(window.settings.active(),'ollama')
        self.assertEqual(str(window.key_entry.cget('state')),'disabled')

if __name__=='__main__': unittest.main()
