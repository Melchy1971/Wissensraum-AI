"""Background worker of the multi-user server (parity roadmap phase 2, decision 10.11.2026).

Runs as its own container from the server image (``python worker.py``), next to the web server. For now
it embeds sections without a vector while semantic search is switched on (``vector_sync.VectorSync``),
so new uploads become findable by meaning without a web request waiting for Ollama. Since phase 3 it
also imports uploaded files (``import_jobs.ImportJobs.create_uploads``), one job after the other. Since
phase 5 it builds the installation export an administrator requested into ``WR_EXPORT_DIR``. Since phase 6
the image carries Tesseract (deu, eng): uploaded images and scans are recognized here, never in the server.

Same database and runtime role as the server, no web port, no sign-in settings. Ollama address from
``WR_OLLAMA_URL``. After every round the worker touches a heartbeat file; the container health check
fails when it is older than three rounds. SIGTERM ends the current batch and stops.

ponytail: one worker. Two would embed the same sections twice (harmless, ``save_vectors`` overwrites).
Upgrade path with phase 3: a job table read with ``FOR UPDATE SKIP LOCKED``.
"""
import os
import signal
import threading
import time
from pathlib import Path

from import_jobs import ImportJobs
from vector_sync import VectorSync

INTERVAL_SECONDS = 30
HEARTBEAT = Path('/tmp/wissensraum-worker.alive')


def run_once(store, should_stop=lambda: False):
    """One round: embed pending sections if semantic search is on and due; returns the state or None."""
    sync = VectorSync(store)
    if not sync.due():
        return None
    return sync.run(should_stop=should_stop)


def import_once(store, stop, heartbeat=HEARTBEAT, beat=INTERVAL_SECONDS):
    """Run the oldest open upload job to its end (or until ``stop``); returns its summary or None.

    The heartbeat is touched after every item and every ``beat`` seconds while one item runs: a scan of many
    pages takes minutes in Tesseract. A hanging Tesseract call is ended by its own time limit per page
    (``ocr.OCR_PAGE_TIMEOUT_SECONDS``), so the worker does not hang for good behind a living heartbeat.
    """
    jobs = ImportJobs(store)
    jobs.drop_kept_uploads()  # bytes of uploads without text after 30 days (decision 11.11.2026)
    job_id = jobs.next_upload_job()
    if job_id is None:
        return None
    done = threading.Event()

    def keep_alive():
        while not done.wait(beat):
            heartbeat.touch()
    beater = threading.Thread(target=keep_alive, daemon=True)
    beater.start()
    try:
        return jobs.run(job_id, pause=stop, progress=lambda *_: heartbeat.touch())  # shutdown: resumed later
    finally:
        done.set()
        beater.join()


def export_once(store, folder, now=None):
    """Build a requested installation export (parity roadmap phase 5); None without an export folder."""
    if not folder:
        return None
    import privacy
    return privacy.run_export(store, folder, time.time() if now is None else now)


def loop(store, stop, log, interval=INTERVAL_SECONDS, heartbeat=HEARTBEAT, rounds=None, export_folder=None):
    """Rounds until ``stop`` is set (or ``rounds`` ran, for tests). A failed round never ends the loop."""
    done = 0
    while not stop.is_set() and (rounds is None or done < rounds):
        try:
            state = run_once(store, should_stop=stop.is_set)
            if state is not None:
                log('vectors_embedded', 'WARNING' if state['failed'] else 'INFO', count=state['embedded'],
                    outcome='failed' if state['failed'] else 'ok')
            summary = import_once(store, stop, heartbeat)
            if summary is not None:
                log('import_job_finished', 'INFO', count=summary['total'], status=summary['status'])
            exported = export_once(store, export_folder)
            if exported and exported.get('status') in ('done', 'failed') and exported.get('finished') == exported.get('started'):
                log('privacy_export', 'INFO', status=exported['status'])
        except Exception as error:  # database away for a moment: the next round tries again
            log('worker_round_failed', 'ERROR', outcome=type(error).__name__)
        heartbeat.touch()
        done += 1
        stop.wait(interval)


def main():  # pragma: no cover - starts the real worker
    import server
    from local_logging import create_operational_logger
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    server.configure_ollama(os.environ)
    database, password = server.database_from_env(os.environ)
    store = server.runtime_store({'database': database, 'db_password': password})
    logger = create_operational_logger(Path(os.environ.get('WR_LOG_DIR') or Path(__file__).resolve().parent),
                                       'worker', known_secrets=(password,) if password else ())

    def log(event, severity='INFO', **metadata):
        if logger is not None:
            try:
                logger.event(event, severity=severity, metadata=metadata or None)
            except Exception:  # logging never stops the worker
                pass
    import ocr
    log('worker_started', status='ocr' if ocr.available() else 'ohne_ocr')
    loop(store, stop, log, int(os.environ.get('WR_WORKER_INTERVAL') or INTERVAL_SECONDS),
         export_folder=os.environ.get('WR_EXPORT_DIR') or None)
    log('worker_stopped')


if __name__ == '__main__':
    main()
