"""Native UI regression; skipped on systems without a graphical display."""
from pathlib import Path
import io
import os
import tempfile
import tkinter as tk
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from settings import Settings
from test_settings import FakeVault

class StartupTests(unittest.TestCase):
    def test_pending_recovery_failure_follows_diagnostics_and_blocks_store_selection(self):
        diagnostics = Mock(return_value=())
        settings = Mock()
        with patch.object(app, 'recover_pending_configuration',
                          side_effect=app.ConfigurationTransferError(
                              'private-safe', 'CONFIG_IMPORT_RECOVERY_INCOMPLETE')), \
             patch.object(app, 'run_diagnostics', diagnostics):
            with self.assertRaises(app.StartupFailure) as raised:
                app.prepare_startup(
                    base=Path('unused'), environ={}, vault_factory=Mock,
                    database_settings_factory=lambda *_: settings)
        diagnostics.assert_called_once()
        settings.open_store.assert_not_called()
        self.assertIn('Wiederherstellungsjournal', str(raised.exception))
        self.assertNotIn('private-safe', str(raised.exception))

    def test_failed_diagnostics_prevent_pending_recovery(self):
        failure = app.DiagnosticResult('database', 'FAIL', 'Datenbank prüfen.', {})
        settings = Mock()
        with patch.object(app, 'run_diagnostics', return_value=(failure,)), \
             patch.object(app, 'recover_pending_configuration') as recover:
            with self.assertRaises(app.StartupFailure):
                app.prepare_startup(
                    base=Path('unused'), environ={}, vault_factory=Mock,
                    database_settings_factory=lambda *_: settings)
        recover.assert_not_called()
        settings.open_store.assert_not_called()

    def test_diagnostics_and_recovery_finish_before_active_store_open(self):
        events = []
        settings = Mock()
        settings.open_store.side_effect = lambda _path: events.append('store') or object()
        diagnostics = lambda *_: events.append('diagnostics') or ()
        recovery = lambda *_: events.append('recovery')

        with patch.object(app, 'recover_pending_configuration', side_effect=recovery):
            _vault, prepared_settings, _results, sqlite_path = app.prepare_startup(
                base=Path('unused'), environ={}, vault_factory=Mock,
                database_settings_factory=lambda *_: settings,
                diagnostics_runner=diagnostics)
        app.initialize_desktop(Mock(), prepared_settings, sqlite_path,
                               lambda: events.append('root'))

        self.assertEqual(events, ['diagnostics', 'recovery', 'root', 'store'])

    def test_production_startup_failure_prevents_root_and_store_creation(self):
        failure = app.DiagnosticResult(
            'database', 'FAIL', 'Datenbank prüfen.',
            {'backend': 'PostgreSQL', 'password': 'SENSITIVE_MARKER'},
        )
        output = io.StringIO()

        with patch.object(app, 'run_diagnostics', return_value=(failure,)), \
             patch.object(app.tk.Tk, '__init__') as root_initializer, \
             patch.object(app.DatabaseSettings, 'open_store') as open_store, \
             patch.object(app, 'messagebox', None), \
             patch.object(app, 'create_operational_logger') as create_logger, \
             redirect_stderr(output):
            result = app.main()

        self.assertEqual(result, 1)
        root_initializer.assert_not_called()
        open_store.assert_not_called()
        create_logger.assert_not_called()
        self.assertIn('Datenbank prüfen.', output.getvalue())
        self.assertNotIn('SENSITIVE_MARKER', output.getvalue())

    def test_actual_root_is_initialized_before_store_open(self):
        events = []
        settings = Mock()
        settings.open_store.side_effect = lambda _path: events.append('store') or object()
        window = Mock()

        app.initialize_desktop(
            window, settings, Path('wissen.db'),
            lambda: events.append('root'),
        )

        self.assertEqual(events, ['root', 'store'])
        window.destroy.assert_not_called()

    def test_store_open_failure_is_redacted_destroys_root_and_has_no_memory_fallback(self):
        settings = Mock()
        settings.open_store.side_effect = RuntimeError('SENSITIVE_MARKER')
        window = Mock()
        with self.assertRaises(app.StartupFailure) as raised:
            app.initialize_desktop(window, settings, Path('wissen.db'), Mock())
        window.destroy.assert_called_once_with()
        self.assertNotIn('SENSITIVE_MARKER', str(raised.exception))
        self.assertNotIn(':memory:', str(raised.exception))

    def test_unavailable_gui_prevents_store_initialization(self):
        settings = Mock()
        window = Mock()
        with self.assertRaises(app.StartupFailure) as raised:
            app.initialize_desktop(
                window, settings, Path('wissen.db'),
                lambda: (_ for _ in ()).throw(RuntimeError('secret display details')),
            )
        settings.open_store.assert_not_called()
        window.destroy.assert_not_called()
        self.assertIn('Tcl/Tk', str(raised.exception))
        self.assertNotIn('secret display details', str(raised.exception))

    def test_post_root_startup_failure_destroys_window(self):
        window = Mock()

        with self.assertRaises(RuntimeError):
            app.run_with_root_cleanup(window, lambda: (_ for _ in ()).throw(
                RuntimeError('SENSITIVE_MARKER')))

        window.destroy.assert_called_once_with()

    def test_startup_failure_has_console_fallback_when_tk_is_unavailable(self):
        failure=app.DiagnosticResult('tcl_tk','FAIL','Tcl/Tk installieren.',{})
        output=io.StringIO()
        with patch.object(app,'tk',None),redirect_stderr(output):
            app.show_startup_failure((failure,))
        self.assertIn('Tcl/Tk installieren.',output.getvalue())

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
        startup_warning=(app.DiagnosticResult('database','WARN','SQLite wird beim ersten Start angelegt.',
                                              {'backend':'SQLite'}),)
        self.patches=[patch.object(app,'BASE',self.base),patch.dict(os.environ,{k:os.environ[k] for k in ('DISPLAY','XAUTHORITY','PATH','SYSTEMROOT','SystemRoot','WINDIR','HOME','USERPROFILE','TCL_LIBRARY','TK_LIBRARY') if k in os.environ},clear=True),
                      patch.object(app,'SystemVault',lambda:self.vault),
                      patch.object(app,'run_diagnostics',return_value=startup_warning),
                      patch.object(app,'Settings',lambda store,base,vault=None:Settings(store,base,self.vault,environ={}))]
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
        self.assertIn('Import',[window.tabs.tab(t,'text') for t in window.tabs.tabs()])
        self.assertIn('E-Mail-Konten',[window.settings_sections.tab(t,'text') for t in window.settings_sections.tabs()])
        self.assertIn('Datenbank',[window.settings_sections.tab(t,'text') for t in window.settings_sections.tabs()])
        self.assertIn('Design',[window.settings_sections.tab(t,'text') for t in window.settings_sections.tabs()])
        self.assertIn('Konfiguration',[window.settings_sections.tab(t,'text') for t in window.settings_sections.tabs()])
        self.assertEqual(window.folder_import_button.cget('text'),'Ordner inkl. Unterordner')
        self.assertEqual(window.key_value.get(),'synthetic-existing-1234567890')
        self.assertEqual(window.key_entry.cget('show'),'*')
        values=window.provider_tree.item('openai','values')
        self.assertEqual(values[0],'AKTIV')
        self.assertIn('7890',values[3])
        self.assertNotIn('synthetic-existing',values[3])
        window.open_settings()
        self.assertEqual(window.tabs.select(),str(window.settings_tab))

    def test_exit_button_closes_application(self):
        window=self.window
        self.assertEqual(window.exit_button.cget('text'),'Beenden')
        with patch.object(window,'destroy') as destroy:
            window.exit_button.invoke()
        destroy.assert_called_once_with()

    def test_startup_diagnostics_show_nonblocking_warning(self):
        self.assertIn('SQLite',self.window.diagnostic_notice.get())
        self.assertIn('Startdiagnose',self.window.diagnostic_notice.get())

    def test_design_mode_and_logo_are_applied(self):
        window=self.window
        logo=self.base/'brand.png'
        from PIL import Image
        Image.new('RGBA',(480,120),'#e20074').save(logo)
        with patch('app.filedialog.askopenfilename',return_value=str(logo)):
            window.select_design_logo()
        self.assertTrue(window.design_settings.logo_path.is_file())
        self.assertIsNotNone(window.logo_image)
        window.design_theme.set('dark')
        window.save_design_theme()
        self.assertEqual(window.design_settings.theme(),'dark')
        self.assertEqual(window.cget('background'),'#121212')
        window.remove_design_logo()
        self.assertFalse(window.design_settings.logo_path.exists())

    def test_damaged_logo_names_the_file_not_the_library(self):
        window=self.window
        window.design_settings.directory.mkdir(parents=True,exist_ok=True)
        window.design_settings.logo_path.write_bytes(b'\x89PNG beschaedigt')
        window.refresh_logo()
        self.assertIsNone(window.logo_image)
        self.assertIn('Gespeichertes Logo ist nicht lesbar',window.logo_load_error)

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

    def test_email_account_is_managed_in_settings(self):
        window=self.window
        window.open_mail_settings()
        window.mail_vars['label'].set('Privat')
        window.mail_vars['email'].set('owner@gmail.com')
        window.mail_vars['username'].set('owner@gmail.com')
        window.mail_vars['secret'].set('synthetic-app-password')
        window.save_mail_account()
        account_id=window.mail_vars['id'].get()
        self.assertTrue(account_id)
        self.assertEqual(window.mail_tree.item(account_id,'values')[0],'Privat')
        self.assertIn('Privat',window.import_account_value.get())
        self.assertEqual(self.vault.get('mail:'+account_id),'synthetic-app-password')

    def test_database_profile_is_stored_without_password(self):
        window=self.window
        values={'host':'db.example.test','port':'5432','database':'wissen_test','username':'test_user',
                'password':'synthetic-db-password','sslmode':'require'}
        for name,value in values.items(): window.database_vars[name].set(value)
        window.save_database_settings()
        saved=window.database_settings.path.read_text(encoding='utf-8')
        self.assertNotIn('synthetic-db-password',saved)
        self.assertEqual(self.vault.get('database:postgresql'),'synthetic-db-password')
        self.assertIn('Aktive Datenquelle bleibt unverändert',window.database_message.get())

    def test_email_preview_requires_explicit_activation(self):
        items=[{'id':'101','date':'15.10.2026','sender':'a@example.com','subject':'Freigabe'},
               {'id':'202','date':'14.10.2026','sender':'b@example.com','subject':'Protokoll','imported':True}]
        preview=self.window.show_mail_preview(items,'synthetic-account','INBOX','Testkonto')
        preview.update()
        self.assertTrue(all(not row['active'] for row in preview.preview_items.values()))
        first=next(iter(preview.preview_items))
        preview.preview_tree.selection_set(first)
        self.window.toggle_preview_selected(preview)
        self.assertTrue(preview.preview_items[first]['active'])
        imported='mail-1'
        self.assertIn('✓ Importiert',preview.preview_tree.item(imported,'values'))
        self.assertIn('imported',preview.preview_tree.item(imported,'tags'))
        preview.preview_tree.selection_set(imported)
        self.window.toggle_preview_selected(preview)
        self.assertFalse(preview.preview_items[imported]['active'])
        self.window.set_preview_active(preview,True)
        self.assertTrue(preview.preview_items[first]['active'])
        self.assertFalse(preview.preview_items[imported]['active'])
        self.assertIn('1 zum Import aktiviert · 1 bereits importiert',preview.preview_status.get())
        self.window.set_preview_active(preview,False)
        self.assertTrue(all(not row['active'] for row in preview.preview_items.values()))
        self.window.close_mail_preview(preview)

if __name__=='__main__': unittest.main()
