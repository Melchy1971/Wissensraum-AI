import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import AI, PostgresStore, Store, chunks, discover_documents, import_document, import_documents, preview_documents, resume_import_documents

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_store_original_and_deduplicate(self):
        doc_id,created = self.store.add('A','Ein Text',filename='a.txt',original=b'Ein Text')
        self.assertTrue(created)
        self.assertEqual(self.store.get(doc_id)['original'],b'Ein Text')
        self.assertEqual(self.store.add('B','Ein Text'),(doc_id,False))
        self.assertEqual(len(self.store.list()),1)

    def test_search_fts_and_special_characters(self):
        a,_ = self.store.add('Prozess','Die Freigabe erfolgt am Dienstag.','Einkauf')
        self.store.add('Sport','Das Turnier startet am Samstag.')
        hits = self.store.search('Wann erfolgt die Freigabe?')
        self.assertEqual(hits[0]['document_id'],a)
        self.assertEqual(self.store.search('" OR * ; --'),[])
        self.assertTrue(self.store.search('Einkauf'))
        self.assertTrue(self.store.search('Prozess'))

    def test_search_uses_semantic_fallback_for_synonyms(self):
        doc_id,_ = self.store.add('Freigabeprozess','Die Führungskraft genehmigt Anträge nach Prüfung.')
        hits = self.store.search('Wer erteilt die Zustimmung?')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['document_id'], doc_id)
        self.assertIn('retrieval_score', hits[0])
        self.assertIn('reranking_score', hits[0])

    def test_delete_removes_fts_and_preserves_other_document(self):
        a,_ = self.store.add('A','Sonderwort Freigabe')
        b,_ = self.store.add('B','Sport Turnier')
        self.store.delete(a)
        self.assertEqual(self.store.search('Sonderwort'),[])
        self.assertEqual(self.store.search('Turnier')[0]['document_id'],b)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            db.execute("INSERT INTO search_index(search_index,rank) VALUES('integrity-check',1)")

    def test_backup_includes_originals_and_index(self):
        self.store.add('A','Wissenssicherung',original=b'original')
        self.store.backup(self.root/'backup.db')
        backup = Store(self.root/'backup.db')
        self.assertEqual(backup.search('Wissenssicherung')[0]['title'],'A')
        self.assertEqual(backup.get(1)['original'],b'original')
        with self.assertRaises(ValueError):
            self.store.backup(self.store.path)

    def test_restore_replaces_current_database_with_verified_backup(self):
        self.store.add('Alt','Vorheriger Stand')
        backup_path = self.root/'backup.db'
        self.store.backup(backup_path)

        self.store.add('Neu','Nachheriger Stand')
        self.store.restore(backup_path)

        restored = Store(self.store.path)
        titles = [row['title'] for row in restored.list()]
        self.assertEqual(titles, ['Alt'])
        self.assertEqual(restored.search('Vorheriger')[0]['title'], 'Alt')
        self.assertEqual(restored.search('Nachheriger'), [])
        with self.assertRaises(ValueError):
            self.store.restore(self.store.path)

    def test_postgres_backup_uses_pg_dump_custom_format(self):
        store = PostgresStore({'host': 'pg.example.test', 'port': 5432,
                               'database': 'wissen', 'username': 'wissen_user',
                               'sslmode': 'require'}, 'postgres-secret', initialize=False)
        target = self.root / 'backup.dump'
        with patch('core.subprocess.run') as run:
            store.backup(target)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'pg_dump')
        self.assertIn('--format=custom', command)
        self.assertIn('--file', command)
        self.assertIn(str(target), command)
        self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], 'postgres-secret')

    def test_postgres_restore_uses_pg_restore_custom_format(self):
        store = PostgresStore({'host': 'pg.example.test', 'port': 5432,
                               'database': 'wissen', 'username': 'wissen_user',
                               'sslmode': 'require'}, 'postgres-secret', initialize=False)
        source = self.root / 'backup.dump'
        source.write_bytes(b'backup')
        with patch('core.subprocess.run') as run:
            store.restore(source)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'pg_restore')
        self.assertIn('--clean', command)
        self.assertIn('--if-exists', command)
        self.assertIn('--dbname', command)
        self.assertIn(str(source), command)
        self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], 'postgres-secret')

    def test_restore_replaces_current_database_with_verified_backup(self):
        self.store.add('Alt','Vorheriger Stand')
        backup_path = self.root/'backup.db'
        self.store.backup(backup_path)

        self.store.add('Neu','Nachheriger Stand')
        self.store.restore(backup_path)

        restored = Store(self.store.path)
        titles = [row['title'] for row in restored.list()]
        self.assertEqual(titles, ['Alt'])
        self.assertEqual(restored.search('Vorheriger')[0]['title'], 'Alt')
        self.assertEqual(restored.search('Nachheriger'), [])
        with self.assertRaises(ValueError):
            self.store.restore(self.store.path)

    def test_utf8_and_cp1252_import(self):
        p = self.root/'test.txt'
        p.write_bytes('Grüße'.encode('cp1252'))
        self.assertEqual(import_document(p)[1],'Grüße')
        p.write_text('Überblick',encoding='utf-8')
        self.assertEqual(import_document(p)[1],'Überblick')

    def test_folder_import_is_recursive_and_keeps_relative_paths(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'/'2026'
        nested.mkdir(parents=True)
        (folder/'Start.md').write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        (nested/'Bild.jpg').write_bytes(b'not-an-import-document')
        documents,skipped,errors=discover_documents(folder)
        self.assertEqual([name for _,name in documents],['Start.md','Projekte/2026/Plan.txt'])
        self.assertEqual((skipped,errors),(1,[]))
        report=import_documents(self.store,documents,skipped,errors)
        self.assertEqual(report,{'found':3,'supported':2,'imported':2,'duplicates':0,
                                 'skipped':1,'errors':0,'details':[]})
        filenames={self.store.get(row['id'])['filename'] for row in self.store.list()}
        self.assertEqual(filenames,{'Start.md','Projekte/2026/Plan.txt'})
        duplicate_report=import_documents(self.store,documents,skipped,errors)
        self.assertEqual(duplicate_report['duplicates'],2)

    def test_folder_import_preview_lists_metadata_without_persisting(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'/'2026'
        nested.mkdir(parents=True)
        (folder/'Start.md').write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)

        preview=preview_documents(documents,skipped,errors)

        self.assertEqual(preview['found'], 2)
        self.assertEqual(preview['supported'], 2)
        self.assertEqual(preview['skipped'], 0)
        self.assertEqual(preview['errors'], 0)
        self.assertEqual([item['filename'] for item in preview['items']], ['Start.md', 'Projekte/2026/Plan.txt'])
        self.assertEqual(preview['items'][0]['title'], 'Start')
        self.assertGreater(preview['items'][0]['size'], 0)
        self.assertNotIn('job_id', preview)
        self.assertEqual(self.store.list(), [])

    def test_resume_import_documents_continues_paused_job(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'
        nested.mkdir(parents=True)
        first=folder/'Start.md'
        second=nested/'Plan.txt'
        first.write_text('Übersicht des Wissensraums',encoding='utf-8')
        second.write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)
        self.store.add('Start','Übersicht des Wissensraums',filename='Start.md',original=first.read_bytes())
        jobs_path = folder/'data'/'import-jobs.json'
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps([{
            'id': 'job-1',
            'source': str(folder),
            'documents': [{'path': str(path), 'filename': name} for path, name in documents],
            'found': 2,
            'supported': 2,
            'processed': 1,
            'imported': 1,
            'duplicates': 0,
            'skipped': 0,
            'errors': 0,
            'status': 'running',
            'started_at': '2026-10-26T00:00:00+00:00',
            'completed_at': None,
            'next_index': 1,
        }], ensure_ascii=False, indent=2), encoding='utf-8')

        report = resume_import_documents(self.store, folder)

        self.assertEqual(report['imported'], 1)
        self.assertEqual(report['duplicates'], 0)
        jobs = json.loads(jobs_path.read_text(encoding='utf-8'))
        self.assertEqual(jobs[0]['processed'], 2)
        self.assertEqual(jobs[0]['imported'], 2)
        self.assertEqual(jobs[0]['next_index'], 2)
        self.assertEqual(jobs[0]['status'], 'done')
        filenames = {self.store.get(row['id'])['filename'] for row in self.store.list()}
        self.assertEqual(filenames, {'Start.md', 'Projekte/Plan.txt'})

    def test_folder_import_reimport_creates_version_history_record(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'
        nested.mkdir(parents=True)
        path=folder/'Start.md'
        path.write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)

        first=import_documents(self.store,documents,skipped,errors,job_source=str(folder))
        second=import_documents(self.store,documents,skipped,errors,job_source=str(folder))

        self.assertEqual(first['imported'], 2)
        self.assertEqual(second['duplicates'], 2)
        jobs = json.loads((folder/'data'/'import-jobs.json').read_text(encoding='utf-8'))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]['status'], 'done')
        self.assertEqual(jobs[1]['status'], 'done')
        filenames = {self.store.get(row['id'])['filename'] for row in self.store.list()}
        self.assertEqual(filenames, {'Start.md', 'Projekte/Plan.txt'})

    def test_folder_import_can_be_cancelled_and_records_partial_job(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'
        nested.mkdir(parents=True)
        (folder/'Start.md').write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)
        snapshots = []
        def hook(report):
            snapshots.append(dict(report))
        def cancel(report):
            return report['processed'] >= 1

        report=import_documents(self.store,documents,skipped,errors,job_source=str(folder), progress_hook=hook, cancel_hook=cancel)

        self.assertEqual(report['imported'], 1)
        self.assertEqual(report['duplicates'], 0)
        self.assertEqual(report['cancelled'], True)
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0]['processed'], 1)
        self.assertEqual(snapshots[-1]['status'], 'cancelled')
        jobs = json.loads((folder/'data'/'import-jobs.json').read_text(encoding='utf-8'))
        self.assertEqual(jobs[0]['processed'], 1)
        self.assertEqual(jobs[0]['imported'], 1)
        self.assertEqual(jobs[0]['status'], 'cancelled')
        filenames = {self.store.get(row['id'])['filename'] for row in self.store.list()}
        self.assertEqual(filenames, {'Start.md'})

    def test_folder_import_updates_progress_during_job(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'
        nested.mkdir(parents=True)
        (folder/'Start.md').write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)
        snapshots = []
        def hook(report):
            snapshots.append(dict(report))

        report=import_documents(self.store,documents,skipped,errors,job_source=str(folder), progress_hook=hook)

        self.assertEqual(report['imported'], 2)
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0]['processed'], 1)
        self.assertEqual(snapshots[0]['imported'], 1)
        self.assertEqual(snapshots[0]['status'], 'running')
        self.assertEqual(snapshots[-1]['processed'], 2)
        self.assertEqual(snapshots[-1]['imported'], 2)
        self.assertEqual(snapshots[-1]['status'], 'done')
        jobs = json.loads((folder/'data'/'import-jobs.json').read_text(encoding='utf-8'))
        self.assertEqual(jobs[0]['processed'], 2)
        self.assertEqual(jobs[0]['imported'], 2)
        self.assertEqual(jobs[0]['status'], 'done')

    def test_folder_import_records_progress_job(self):
        folder=self.root/'Ablage'
        nested=folder/'Projekte'
        nested.mkdir(parents=True)
        (folder/'Start.md').write_text('Übersicht des Wissensraums',encoding='utf-8')
        (nested/'Plan.txt').write_text('Projektplan mit Meilensteinen',encoding='utf-8')
        documents,skipped,errors=discover_documents(folder)

        report=import_documents(self.store,documents,skipped,errors,job_source=str(folder))

        self.assertIn('job_id', report)
        jobs_path = folder/'data'/'import-jobs.json'
        self.assertTrue(jobs_path.is_file())
        jobs = json.loads(jobs_path.read_text(encoding='utf-8'))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job['id'], report['job_id'])
        self.assertEqual(job['source'], str(folder))
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['found'], 2)
        self.assertEqual(job['supported'], 2)
        self.assertEqual(job['imported'], 2)
        self.assertEqual(job['duplicates'], 0)
        self.assertEqual(job['skipped'], 0)
        self.assertEqual(job['errors'], 0)

    def test_docx_tables(self):
        from docx import Document
        p = self.root/'test.docx'
        doc = Document()
        doc.add_paragraph('Prozesswissen')
        table = doc.add_table(rows=1,cols=2)
        table.cell(0,0).text='Rolle'
        table.cell(0,1).text='Verantwortlich'
        doc.save(p)
        result = import_document(p)[1]
        self.assertIn('Prozesswissen',result)
        self.assertIn('Rolle | Verantwortlich',result)

    def test_empty_pdf_rejected(self):
        from pypdf import PdfWriter
        p = self.root/'leer.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=100,height=100)
        with p.open('wb') as f:
            writer.write(f)
        with self.assertRaisesRegex(ValueError,'OCR'):
            import_document(p)

    def test_chunk_overlap_coverage(self):
        text = ''.join(str(i%10) for i in range(8000))
        parts = list(chunks(text))
        self.assertEqual(parts[0]+''.join(p[200:] for p in parts[1:]),text)
        self.assertTrue(all(len(p)<=1800 for p in parts))

    def test_question_sends_only_retrieved_sources_and_logs_snapshot(self):
        a,_ = self.store.add('Einkauf','Freigabe erfolgt durch die Führungskraft.')
        self.store.add('Privat','Geheimes anderes Thema ohne Treffer.')
        calls = []
        def transport(req, timeout):
            payload = json.loads(req.data)
            calls.append(payload)
            return io.BytesIO(json.dumps({'status':'completed','output':[{'content':[
                {'type':'output_text','text':'Die Führungskraft gibt frei. [Q1]'}]}]}).encode())
        answer,sources = AI(self.store,'test-key',transport=transport).ask('Wer macht die Freigabe?')
        self.assertIn('[Q1]',answer)
        self.assertEqual(len(sources),1)
        self.assertFalse(calls[0]['store'])
        self.assertNotIn('Geheimes',calls[0]['input'])
        self.store.delete(a)
        history = self.store.history()
        self.assertEqual(len(history),1)
        self.assertIn('Führungskraft',history[0]['sources_json'])

    def test_no_sources_never_calls_api(self):
        def fail(*args,**kwargs):
            raise AssertionError('API must not be called')
        answer,sources = AI(self.store,'key',transport=fail).ask('Unbekannt')
        self.assertEqual(sources,[])
        self.assertIn('Keine passenden',answer)
        self.assertEqual(self.store.history(),[])

    def test_invalid_citation_not_saved(self):
        self.store.add('A','Freigabe')
        def transport(*args,**kwargs):
            return io.BytesIO(json.dumps({'output':[{'content':[{'type':'output_text','text':'Fakt [Q99]'}]}]}).encode())
        with self.assertRaisesRegex(ValueError,'ungültige Quellen'):
            AI(self.store,'key',transport=transport).ask('Freigabe')
        self.assertEqual(self.store.history(),[])

    def test_summary_and_length_guard(self):
        a,_ = self.store.add('A','Prozesswissen')
        def transport(*args,**kwargs):
            return io.BytesIO(json.dumps({'output':[{'content':[{'type':'output_text','text':'Wissen. [Q1]'}]}]}).encode())
        answer,sources = AI(self.store,'key',transport=transport).summarize(a)
        self.assertEqual(len(sources),1)
        self.assertEqual(self.store.history()[0]['kind'],'summary')
        b,_ = self.store.add('Lang','a'*24001)
        with self.assertRaises(ValueError):
            AI(self.store,'key',transport=transport).summarize(b)

    def test_api_errors_redacted(self):
        self.store.add('A','Freigabe')
        def transport(req,**kwargs):
            raise urllib.error.HTTPError(req.full_url,401,'secret error',{},None)
        with self.assertRaisesRegex(ValueError,'ungültig') as error:
            AI(self.store,'SECRET-KEY',transport=transport).ask('Freigabe')
        self.assertNotIn('SECRET',str(error.exception))

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            self.store.add('','leer')
        with self.assertRaises(ValueError):
            AI(self.store,'key').ask('')

if __name__ == '__main__':
    unittest.main()
