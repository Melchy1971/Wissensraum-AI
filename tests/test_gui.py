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
            self.window=None
        # Free this test's Tk objects now, in the main thread. Otherwise a later garbage collection
        # can run them in a worker thread of the next test, where every Tk variable waits about one
        # second for a main loop that no longer exists and the worker misses the test's time limit.
        import gc; gc.collect()
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

    def test_toolbar_buttons_stay_fully_visible_at_minimum_width(self):
        window=self.window
        width,height=window.minsize()
        window.geometry(f'{width}x{height}')
        window.update()
        for button in window.exit_button.master.winfo_children():
            with self.subTest(button=button.cget('text')):
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                                     window.winfo_rootx()+window.winfo_width())
        def texts(widget):
            yield widget.cget('text') if widget.winfo_class()=='TButton' else None
            for child in widget.winfo_children():
                yield from texts(child)
        self.assertLessEqual({'Sicherung wiederherstellen','Systemstatus prüfen'},
                             set(texts(window.database_settings_frame)))

    def test_settings_pages_scroll_to_their_last_widget_at_minimum_size(self):
        window=self.window
        width,height=window.minsize()
        window.geometry(f'{width}x{height}')
        window.open_settings()
        window.database_message.set('Kennwort: Betriebssystem-Schlüsselspeicher')
        for name in ('ai','database','mail','design','configuration'):
            content=getattr(window,f'{name}_settings_frame')
            canvas=content.master
            with self.subTest(page=name):
                window.select_settings_page(content)
                canvas.yview_moveto(0)
                window.update()
                if name=='database':
                    self.assertLess(canvas.yview()[1],1.0)  # content taller than the page
                canvas.yview_moveto(1.0)
                window.update()
                bottom=max(child.winfo_y()+child.winfo_height() for child in content.winfo_children())
                self.assertLessEqual(bottom-canvas.canvasy(0),canvas.winfo_height()+1)
                self.assertEqual(canvas.cget('background'),window.palette['background'])
                canvas.xview_moveto(0)
                window.update()
                right=canvas.winfo_rootx()+canvas.winfo_width()
                def widgets(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from widgets(child)
                self.assertEqual(content.winfo_width(),canvas.winfo_width())
                for widget in widgets(content):
                    if widget.winfo_class() in ('TLabel','TButton') and widget.winfo_ismapped():
                        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_reqwidth(),right+1,
                                             widget.cget('text'))
        window.design_theme.set('dark')
        window.save_design_theme()
        self.assertEqual({canvas.cget('background') for canvas in window.settings_canvases},{'#121212'})

    def wait_until_idle(self,window,timeout=30):
        import time
        deadline=time.monotonic()+timeout
        window.update()
        while window.busy and time.monotonic()<deadline:
            window.update(); time.sleep(0.02)
        window.update()
        self.assertFalse(window.busy)

    def test_folder_import_runs_as_cancellable_job_and_reports_result(self):
        window=self.window
        folder=self.base/'quelle'; (folder/'unter').mkdir(parents=True)
        (folder/'a.txt').write_text('Auftrag eins',encoding='utf-8')
        (folder/'unter'/'b.md').write_text('Auftrag zwei',encoding='utf-8')
        (folder/'c.exe').write_bytes(b'MZ')
        self.assertEqual(str(window.import_cancel_button.cget('state')),'disabled')
        with patch('app.filedialog.askdirectory',return_value=str(folder)), \
             patch('app.messagebox.showinfo') as info:
            window.import_local_folder()
            self.assertEqual(str(window.import_cancel_button.cget('state')),'normal')
            self.wait_until_idle(window)
            preview=window.import_preview_window
            self.assertEqual(window.store.list(),[])  # the preview stores nothing
            self.assertEqual(len(preview.preview_tree.get_children()),3)
            preview.start_import()
            self.wait_until_idle(window)
        self.assertFalse(preview.winfo_exists())
        self.assertIn('2 importiert',info.call_args.args[1])
        self.assertIn('1 in Quarantäne',window.import_message.get())
        self.assertEqual(str(window.import_cancel_button.cget('state')),'disabled')
        self.assertIsNone(window.import_cancel)
        report=self.base/'bericht.csv'
        with patch('app.filedialog.asksaveasfilename',return_value=str(report)):
            window.save_import_report()
            self.wait_until_idle(window)
        self.assertIn('Dateityp nicht unterstützt',report.read_text(encoding='utf-8-sig'))
        with patch('app.messagebox.showinfo') as info:
            window.resume_import_job()
        self.assertIn('keinen offenen Importauftrag',info.call_args.args[1])

    def test_preview_shows_title_type_size_excerpt_and_outcome_before_saving(self):
        window=self.window
        folder=self.base/'quelle'; folder.mkdir()
        (folder/'a.txt').write_text('Erste Fassung des Plans',encoding='utf-8')
        with patch('app.filedialog.askdirectory',return_value=str(folder)), patch('app.messagebox.showinfo'):
            window.import_local_folder(); self.wait_until_idle(window)
            window.import_preview_window.start_import(); self.wait_until_idle(window)
        (folder/'a.txt').write_text('Zweite Fassung des Plans',encoding='utf-8')
        (folder/'b.txt').write_text('Erste Fassung des Plans',encoding='utf-8')
        (folder/'c.exe').write_bytes(b'MZ')
        with patch('app.filedialog.askdirectory',return_value=str(folder)):
            window.import_local_folder(); self.wait_until_idle(window)
        preview=window.import_preview_window
        rows={preview.preview_tree.item(iid,'values')[1]:preview.preview_tree.item(iid,'values')
              for iid in preview.preview_tree.get_children()}
        self.assertEqual(rows['a.txt'][0],'JA')  # everything is selected at first
        self.assertEqual(rows['a.txt'][2:5],('txt','24 B','a'))
        self.assertEqual(rows['a.txt'][5],'neue Version 2 von „a“')
        self.assertEqual(rows['b.txt'][5],'bereits vorhanden')
        self.assertEqual(rows['c.exe'][5],'Quarantäne: Dateityp nicht unterstützt')
        self.assertIn('Zweite Fassung',preview.preview_excerpt.get('1.0','end'))
        self.assertIn('Noch ist nichts gespeichert',preview.preview_summary)
        self.assertEqual(window.preview_outcome({'outcome':'ocr','pages':3}),'wird beim Import per OCR gelesen (3 Seiten)')
        self.assertEqual(window.preview_outcome({'outcome':'ocr','pages':1}),'wird beim Import per OCR gelesen (1 Seite)')
        self.assertEqual(len(window.store.list(all_versions=True)),1)
        with patch('app.messagebox.showinfo') as info:
            preview.start_import(); self.wait_until_idle(window)
        self.assertIn('1 neue Version',info.call_args.args[1])
        current=window.store.list()
        self.assertEqual(len(current),1)
        window.tree.selection_set(str(current[0]['id'])); window.update()
        self.assertIn('Version 2 von 2',window.detail.get('1.0','end'))
        versions=window.show_versions()
        self.assertEqual([versions.version_tree.item(iid,'values')[0] for iid in versions.version_tree.get_children()],
                         ['2','1'])
        versions.version_tree.selection_set(versions.version_tree.get_children()[1]); window.update()
        self.assertIn('Erste Fassung',versions.version_text.get('1.0','end'))
        versions.destroy()

    def test_preview_buttons_stay_visible_and_selection_limits_the_import(self):
        window=self.window
        folder=self.base/'quelle'; folder.mkdir()
        for index in range(40):
            (folder/f'datei-{index:02}.txt').write_text(f'Inhalt {index}',encoding='utf-8')
        with patch('app.filedialog.askdirectory',return_value=str(folder)):
            window.import_local_folder(); self.wait_until_idle(window)
        preview=window.import_preview_window
        mail=window.show_mail_preview([{'id':'1','subject':'x','sender':'a','date':'d','attachments':[]}],
                                      'synthetic-account','INBOX','Konto')
        for dialog in (preview,mail):
            for size in ('1080x650','820x480'):
                dialog.geometry(size); dialog.update()
                button=dialog.import_button
                with self.subTest(dialog=dialog.title(),size=size):
                    self.assertTrue(button.winfo_ismapped())
                    bottom=button.winfo_rooty()+button.winfo_height()-dialog.winfo_rooty()
                    self.assertLessEqual(bottom,dialog.winfo_height())
        window.close_mail_preview(mail)
        tree=preview.preview_tree
        self.assertIn('40 von 40 Dateien',preview.preview_status.get())
        preview.set_rows(range(40),False)
        self.assertEqual(str(preview.import_button.cget('state')),'disabled')
        preview.set_rows([0,1,2],True)
        tree.selection_set('1'); preview.set_rows([int(iid) for iid in tree.selection()])  # "Markierte umschalten"
        self.assertEqual([tree.item(str(index),'values')[0] for index in range(4)],['JA','NEIN','JA','NEIN'])
        self.assertIn('2 von 40 Dateien',preview.preview_status.get())
        with patch('app.messagebox.showinfo') as info:
            preview.start_import(); self.wait_until_idle(window)
        self.assertIn('2 Dateien · 2 importiert',info.call_args.args[1])
        self.assertEqual(sorted(row['title'] for row in window.store.list()),['datei-00','datei-02'])
        job=window.import_jobs.jobs(1)[0]
        self.assertEqual(sorted(item['locator'] for item in window.import_jobs.items(job['job_id'])),
                         ['datei-00.txt','datei-02.txt'])

    def test_rules_page_previews_applies_filters_and_reverts(self):
        window=self.window
        self.assertIn('Regeln',[window.settings_sections.tab(t,'text') for t in window.settings_sections.tabs()])
        doc_id=window.store.add('Angebot','Betreff: Angebot\nVon: Erika <erika@kunde.example>\n\nText','E-Mail · Konto',
                                'Angebot.eml',b'raw',('imap','konto','INBOX/1'))[0]
        page=window.rules_page
        page.new()
        page.values['name'].set('Kunde')
        with patch('rules_view.messagebox.showerror') as error:
            page.save()  # no condition yet
        self.assertIn('Bedingung',error.call_args.args[1])
        page.values['sender_domain'].set('@Kunde.Example')
        page.values['category'].set('Kunden')
        page.values['tags'].set('Kunde, Angebot')
        page.save()
        self.assertEqual(page.tree.item(str(page.selected_id),'values')[3],'Absender @kunde.example')
        self.assertEqual(window.store.get(doc_id)['category'],'E-Mail · Konto')  # saving a rule changes nothing
        page.preview(); self.wait_until_idle(window)
        preview=page.preview_window
        self.assertEqual(preview.preview_tree.item('0','values'),('Angebot','E-Mail · Konto','Kunden','Kunde, Angebot'))
        preview.confirm_button.invoke(); self.wait_until_idle(window)
        self.assertEqual(window.store.get(doc_id)['category'],'Kunden')
        self.assertEqual(window.tree.item(str(doc_id),'values')[2],'Angebot, Kunde')
        self.assertIn('Kunde (1)',window.tag_box.cget('values'))
        window.store.add('Ohne Tag','Anderer Text')
        window.tag_filter.set('Kunde (1)'); window.refresh()
        self.assertEqual(window.tree.get_children(),(str(doc_id),))
        window.tree.selection_set(str(doc_id)); window.show_document()
        self.assertIn('Tags: Angebot, Kunde',window.detail.get('1.0','end'))
        window.tag_filter.set(app.ALL_TAGS); window.refresh()
        self.assertEqual(len(window.tree.get_children()),2)
        page.runs.selection_set(page.runs.get_children()[0])
        with patch('rules_view.messagebox.askyesno',return_value=True):
            page.revert(); self.wait_until_idle(window)
        self.assertEqual(window.store.get(doc_id)['category'],'E-Mail · Konto')
        self.assertIn('zurückgenommen',page.runs.item(page.runs.get_children()[0],'values')[3])
        self.assertEqual(window.tag_box.cget('values'),(app.ALL_TAGS,))

    def test_cancelled_preview_stores_nothing(self):
        window=self.window
        folder=self.base/'quelle'; folder.mkdir()
        (folder/'a.txt').write_text('Inhalt',encoding='utf-8')
        real_preview=window.import_jobs.preview_folder
        def cancel_early(folder,cancel=None,progress=None):
            cancel.set()
            return real_preview(folder,cancel,progress)
        with patch('app.filedialog.askdirectory',return_value=str(folder)), \
             patch.object(window.import_jobs,'preview_folder',side_effect=cancel_early):
            window.import_local_folder(); self.wait_until_idle(window)
        self.assertIn('Vorschau abgebrochen',window.import_message.get())
        self.assertEqual(window.store.list(all_versions=True),[])
        self.assertEqual(window.import_jobs.jobs(),[])
        self.assertIsNone(window.import_cancel)

    def test_cancelled_import_job_can_be_resumed_from_the_interface(self):
        window=self.window
        folder=self.base/'quelle'; folder.mkdir()
        for index in range(5):
            (folder/f'{index}.txt').write_text(f'Datei {index}',encoding='utf-8')
        real_run=window.import_jobs.run
        def cancel_early(job_id,cancel=None,progress=None):
            cancel.set()  # as if "Import abbrechen" was pressed before the first file
            return real_run(job_id,cancel,progress)
        with patch('app.filedialog.askdirectory',return_value=str(folder)), \
             patch('app.messagebox.showinfo') as info, \
             patch.object(window.import_jobs,'run',side_effect=cancel_early):
            window.import_local_folder()
            self.wait_until_idle(window)
            window.import_preview_window.start_import()
            self.wait_until_idle(window)
        self.assertIn('abgebrochen',info.call_args.args[1])
        self.assertIn('Auftrag fortsetzen',info.call_args.args[1])
        with patch('app.messagebox.showinfo') as info:
            window.resume_import_job()
            self.wait_until_idle(window)
        self.assertIn('5 importiert',info.call_args.args[1])

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
        window.mail_vars['username'].set('owner')  # ignored for Gmail: the address is the user name
        window.mail_vars['secret'].set('Normales-Passwort1')
        with patch('app.messagebox.showerror') as error:
            window.save_mail_account()
        self.assertIn('App-Passwort aus 16 Kleinbuchstaben',str(error.call_args))
        self.assertFalse(window.mail_vars['id'].get())
        window.mail_vars['secret'].set('abcd efgh ijkl mnop')
        window.save_mail_account()
        account_id=window.mail_vars['id'].get()
        self.assertTrue(account_id)
        self.assertEqual(window.mail_tree.item(account_id,'values')[0],'Privat')
        self.assertIn('Privat',window.import_account_value.get())
        self.assertEqual(self.vault.get('mail:'+account_id),'abcdefghijklmnop')
        self.assertEqual(window.mail_accounts.get(account_id)['username'],'owner@gmail.com')
        self.assertEqual(str(window.mail_fields['username'].cget('state')),'disabled')

    def test_mail_sync_is_activated_per_account_and_runs_from_the_timer(self):
        from mail_fakes import FakeImapServer, mail
        window=self.window
        panel=window.sync_panel
        self.assertIn('Konto zuerst speichern',panel.status.get())
        account_id=window.mail_accounts.save({'label':'Arbeit','provider':'imap','email':'m@example.test','username':'m',
                                              'host':'imap.example.test','port':993},'geheim')
        server=FakeImapServer({7:mail('Alt','Vorher da.')})
        real=window.importer._imap_login
        window.importer._imap_login=lambda account,folder,factory=None:real(account,folder,server.client)
        window.refresh_mail_accounts(account_id)
        self.assertIn('ist aus',panel.status.get())
        clock=[1_000_000.0]; panel.sync.clock=lambda:clock[0]
        panel.interval.set('15 Min.')
        panel.activate(); self.wait_until_idle(window)
        self.assertIn('Aktiv, alle 15 Min., Ordner INBOX',panel.status.get())
        server.raw['8']=mail('Neu','Nach der Aktivierung.')
        panel.tick(); self.assertFalse(panel.running)  # not due yet
        clock[0]+=15*60
        panel.tick()
        import time
        deadline=time.monotonic()+20
        while (panel.running or panel.pending is None) and time.monotonic()<deadline:
            time.sleep(0.02)
        panel.poll()
        self.assertEqual(window.status.get(),'Synchronisation: 1 neue E-Mails')
        self.assertEqual([row['title'] for row in window.store.list()],['Neu'])
        panel.deactivate()
        self.assertIn('ist aus',panel.status.get())

    def test_semantic_search_is_off_until_activated_and_embeds_from_the_timer(self):
        from test_vector_sync import FakeEmbedder
        import time
        FakeEmbedder.calls, FakeEmbedder.offline = [], False
        window=self.window
        panel=window.vector_panel
        panel.sync.embedder=FakeEmbedder
        window.store.add('Heizung','Wartung im März')
        panel.load()
        self.assertIn('ist aus. 0 von 1',panel.status.get())
        panel.tick()
        self.assertFalse(panel.running)
        self.assertEqual(FakeEmbedder.calls,[])  # nothing is sent before activation
        panel.activate(); self.wait_until_idle(window)
        self.assertIn('Aktiv mit qwen3-embedding:0.6b. 0 von 1',panel.status.get())
        panel.tick()
        deadline=time.monotonic()+20
        while (panel.running or panel.pending is None) and time.monotonic()<deadline:
            time.sleep(0.02)
        panel.poll()
        self.assertIn('1 von 1 Abschnitten eingebettet',panel.status.get())
        self.assertEqual(window.status.get(),'Semantische Suche: 1 Abschnitte eingebettet')
        panel.deactivate()
        self.assertIn('ist aus. 1 von 1',panel.status.get())

    def test_search_shows_hybrid_status_and_falls_back_to_full_text(self):
        from test_retrieval import WordEmbedder, embed_all
        window=self.window
        WordEmbedder.offline=False
        window.store.add('Wartungsplan','Die Therme bekommt jährlich eine Wartung.')
        window.retriever.embedder=WordEmbedder
        window.query.set('Wartung'); window.refresh(); self.wait_until_idle(window)
        self.assertIn('Volltextsuche',window.status.get())
        embed_all(window.store)
        window.query.set('Heizung'); window.refresh(); self.wait_until_idle(window)
        self.assertEqual(window.tree.get_children(),(str(window.store.list()[0]['id']),))
        self.assertIn('Hybride Suche',window.status.get())
        self.assertNotIn('rrf',window.status.get().lower())
        WordEmbedder.offline=True
        window.query.set('Wartung im März'); window.refresh(); self.wait_until_idle(window)
        self.assertIn('Volltext-Fallback – semantisches Modell nicht erreichbar',window.status.get())
        self.assertEqual(len(window.tree.get_children()),1)
        WordEmbedder.offline=False

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

    def test_attachments_are_released_one_by_one_and_never_by_select_all(self):
        pdf={'id':'2','name':'vertrag.pdf','content_type':'application/pdf','size':2048}
        png={'id':'3','name':'foto.png','content_type':'image/png','size':900,'imported':True}
        items=[{'id':'101','date':'23.10.2026','sender':'a@example.com','subject':'Vertrag','attachments':[pdf,png]},
               {'id':'202','date':'22.10.2026','sender':'b@example.com','subject':'Alt','imported':True,
                'attachments':[{'id':'2','name':'anlage.docx','content_type':'','size':10}]}]
        preview=self.window.show_mail_preview(items,'synthetic-account','INBOX','Testkonto')
        preview.update()
        tree=preview.preview_tree
        self.assertEqual(tree.get_children('mail-0'),('mail-0:0','mail-0:1'))
        self.assertEqual(tree.item('mail-0:0','values')[3:],('Anhang','vertrag.pdf · application/pdf · 2.0 KB'))
        self.assertIn('✓ Importiert',tree.item('mail-0:1','values'))
        self.window.set_preview_active(preview,True)  # e-mails only
        self.assertFalse(any(row['active'] for row in preview.preview_items.values() if row['attachment']))
        self.assertIn('0 von 3 Anhängen freigegeben',preview.preview_status.get())
        self.window.set_preview_active(preview,False)
        self.window.toggle_preview_selected(preview,('mail-0:0',))  # release brings the e-mail along
        self.assertTrue(preview.preview_items['mail-0:0']['active'])
        self.assertTrue(preview.preview_items['mail-0']['active'])
        self.window.toggle_preview_selected(preview,('mail-0:1','mail-1:0'))  # imported row stays off
        self.assertFalse(preview.preview_items['mail-0:1']['active'])
        self.assertFalse(preview.preview_items['mail-1']['active'])  # e-mail already imported
        self.assertTrue(preview.preview_items['mail-1:0']['active'])
        self.window.toggle_preview_selected(preview,('mail-0',))  # without its e-mail, no attachment
        self.assertFalse(preview.preview_items['mail-0:0']['active'])
        self.window.toggle_preview_selected(preview,('mail-0:0',))
        created=[]
        with patch.object(self.window.importer,'create_mail_job',
                          side_effect=lambda *args:created.append(args) or 1/0):
            with patch('app.messagebox.showerror'):
                self.window.authorize_mail_import(preview,'synthetic-account','INBOX')
                self.wait_until_idle(self.window)
        jobs,account_id,folder,selected,released=created[0]
        self.assertEqual((account_id,folder),('synthetic-account','INBOX'))
        self.assertEqual([item['id'] for item in selected],['101'])
        self.assertEqual(released,{'101':['2'],'202':['2']})
        self.assertIsNone(self.window.import_cancel)  # a failed start resets the job controls
        self.assertEqual(self.window.report_text({'found':1,'imported':1,'duplicates':0,'errors':0,
            'attachments_imported':1,'attachments_duplicates':0,'attachments_quarantined':1,'attachments_errors':0,
            'quarantine':[{'name':'foto.png','reason':'no_text'}]}),
            '1 gefunden · 1 importiert · 0 bereits vorhanden · 0 Fehler · Anhänge: 1 importiert, 0 bereits vorhanden, '
            '1 in Quarantäne, 0 Fehler (foto.png: Kein lesbarer Text)')
        self.window.close_mail_preview(preview)

    def test_old_mail_attachments_are_removed_only_after_confirmation(self):
        import import_fixtures as fx
        from mail_fakes import mail
        window=self.window
        raw=mail('Altmail','Alter Text.',[('alt.pdf','application/pdf',fx.pdf_with_text('Alt'))])
        document_id,_=window.importer._add_email(raw,'Arbeit',('imap','','imap:INBOX:1'))
        with patch('app.messagebox.askyesno',return_value=False) as ask:
            window.strip_old_mail_attachments(); self.wait_until_idle(window)
        self.assertIn('1 mit Anhängen im Original',ask.call_args.args[1])
        self.assertEqual(window.store.get(document_id)['original'],raw)  # declined: unchanged
        with patch('app.messagebox.askyesno',return_value=True), patch('app.messagebox.showinfo') as info:
            window.strip_old_mail_attachments(); self.wait_until_idle(window)
            self.wait_until_idle(window)
        self.assertIn('Sicherung:',info.call_args.args[1])
        self.assertIn(b'X-Wissensraum-Anhaenge-nicht-gespeichert: 1',window.store.get(document_id)['original'])

    def test_retry_of_quarantined_scans_waits_for_ocr_and_runs_once_available(self):
        import os
        import import_fixtures as fx
        window=self.window
        folder=self.base/'scans'; folder.mkdir()
        (folder/'leer.png').write_bytes(fx.blank_png())
        with patch('app.messagebox.showinfo') as info:
            window.retry_no_text()
        self.assertIn('Kein Eintrag wartet',info.call_args.args[1])
        with patch.dict(os.environ,{'WISSENSRAUM_OCR':'0'}):
            window.import_jobs.run(window.import_jobs.create_for_folder(folder))
            with patch('app.messagebox.showinfo') as info:
                window.retry_no_text()
            self.assertIn('1 Einträge warten auf Texterkennung',info.call_args.args[1])
            self.assertIn('Bilder und Scans ohne Text landen in Quarantäne',window.ocr_notice.get())
        self.assertEqual(len(window.import_jobs.jobs()),1)  # no retry job without OCR
        with patch('ocr.available',return_value=True), patch('app.messagebox.showinfo') as info:
            window.retry_no_text(); self.wait_until_idle(window)
        self.assertEqual(len(window.import_jobs.jobs()),2)
        self.assertIn('1 Dateien',info.call_args.args[1])

    def test_pst_import_runs_as_job_with_released_attachment_only(self):
        import import_fixtures as fx
        from mail_fakes import FakePstArchive, mail
        archive=FakePstArchive([
            (mail('Archiv Bericht','Bericht im Archiv.'),[('bericht.pdf','application/pdf',fx.pdf_with_text('Archivanhang')),
                                                          ('foto.png','image/png',b'\x89PNGx')]),
            (mail('Archiv Notiz','Notiz im Archiv.'),[])])
        path=self.base/'archiv.pst'; path.write_bytes(b'!BDN')
        window=self.window
        with patch('app.filedialog.askopenfilename',return_value=str(path)), \
             patch.object(window.importer,'_pst_archive',side_effect=lambda p,factory=None:archive):
            window.import_pst_file(); self.wait_until_idle(window)
            preview=window.mail_preview_window
            self.assertEqual(preview.preview_tree.get_children('mail-0'),('mail-0:0','mail-0:1'))
            self.assertEqual(archive.read_log,[])
            window.toggle_preview_selected(preview,('mail-0:0','mail-1'))
            with patch('app.messagebox.showinfo') as info:
                window.authorize_mail_import(preview,None,str(path))
                self.wait_until_idle(window)
        self.assertEqual(archive.read_log,['bericht.pdf'])
        self.assertIn('3 Einträge · 3 importiert',info.call_args.args[1])
        self.assertTrue(window.import_message.get().startswith('PST: Auftrag'))
        self.assertFalse(preview.winfo_exists())
        titles={row['title'] for row in window.store.list()}
        self.assertEqual(titles,{'Archiv Bericht','Archiv Notiz','bericht'})

if __name__=='__main__': unittest.main()
