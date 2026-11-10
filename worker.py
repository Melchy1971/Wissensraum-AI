"""Background worker of the multi-user server (parity roadmap phase 2, decision 10.11.2026).

Runs as its own container from the server image (``python worker.py``), next to the web server. For now
it embeds sections without a vector while semantic search is switched on (``vector_sync.VectorSync``),
so new uploads become findable by meaning without a web request waiting for Ollama. Since phase 3 it
also imports uploaded files (``import_jobs.ImportJobs.create_uploads``), one job after the other.

Same database and runtime role as the server, no web port, no sign-in settings. Ollama address from
``WR_OLLAMA_URL``. After every round the worker touches a heartbeat file; the container health check
fails when it is older than three rounds. SIGTERM ends the current batch and stops.

ponytail: one worker. Two would embed the same sections twice (harmless, ``save_vectors`` overwrites).
Upgrade path with phase 3: a job table read with ``FOR UPDATE SKIP LOCKED``.
"""
import os
import signal
import threading
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


def import_once(store, stop, heartbeat=HEARTBEAT):
    """Run the oldest open upload job to its end (or until ``stop``); returns its summary or None.

    The heartbeat is touched after every item, so a long job does not look like a hanging worker.
    """
    jobs = ImportJobs(store)
    job_id = jobs.next_upload_job()
    if job_id is None:
        return None
    return jobs.run(job_id, pause=stop, progress=lambda *_: heartbeat.touch())  # shutdown: resumed later


def loop(store, stop, log, interval=INTERVAL_SECONDS, heartbeat=HEARTBEAT, rounds=None):
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
    log('worker_started')
    loop(store, stop, log, int(os.environ.get('WR_WORKER_INTERVAL') or INTERVAL_SECONDS))
    log('worker_stopped')


if __name__ == '__main__':
    main()
