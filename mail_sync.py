"""Mailbox sync while the application is open (Release 2.0.1, step S3).

Each account is synced only after explicit activation. Activation remembers where the
mailbox stands (IMAP: UIDVALIDITY and highest UID of one folder; Microsoft 365: receive
time of the newest inbox message); later runs import only messages that arrive after that
point, as an ordinary import job without attachments. Attachments are never fetched
automatically. A sign-in problem stops the sync for that account with its reason until it is
activated again; an unreachable server is retried at the next interval.

There is no background service: ``MailSync.run_due`` is called by a timer of the open
application. Settings and state live in the ``settings`` table as JSON (name
``mail_sync.<account id>``), without credentials.
"""
from __future__ import annotations

import json
import time

from importer import MailLoginError

INTERVALS = (15, 30, 60, 240)
DEFAULT_INTERVAL = 60
BATCH_LIMIT = 200
PREFIX = 'mail_sync.'


def interval_label(minutes):
    return f'{minutes // 60} Std.' if minutes >= 60 and minutes % 60 == 0 and minutes > 60 else f'{minutes} Min.'


class MailSync:
    def __init__(self, store, importer, jobs, clock=time.time):
        self.store, self.importer, self.jobs, self.clock = store, importer, jobs, clock

    # --- state -------------------------------------------------------------------------------
    def config(self, account_id):
        with self.store.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?', (PREFIX + account_id,)).fetchone()
        state = {'enabled': False, 'interval': DEFAULT_INTERVAL, 'folder': 'INBOX', 'mark': None,
                 'stopped': '', 'last_run': None, 'last_result': ''}
        if row:
            try:
                state.update(json.loads(row['value']))
            except (TypeError, ValueError):
                pass
        return state

    def _save(self, account_id, state):
        with self.store.connect() as db:
            db.execute('DELETE FROM settings WHERE name=?', (PREFIX + account_id,))
            db.execute('INSERT INTO settings(name,value) VALUES(?,?)',
                       (PREFIX + account_id, json.dumps(state, ensure_ascii=False, sort_keys=True)))

    def accounts(self):
        """Every existing account with its sync state; states of deleted accounts are ignored."""
        return [(account, self.config(account['id'])) for account in self.importer.accounts.list()]

    # --- activation --------------------------------------------------------------------------
    def activate(self, account_id, interval=DEFAULT_INTERVAL, folder='INBOX'):
        """Enable sync from now on; reads only where the mailbox stands (raises on sign-in errors)."""
        interval = int(interval)
        if interval not in INTERVALS:
            raise ValueError('Intervall: 15, 30 oder 60 Minuten oder 4 Stunden.')
        account = self.importer.accounts.get(account_id)
        if account['provider'] == 'microsoft':
            folder = 'INBOX'  # Microsoft 365: inbox only
            mark = {'after': self.importer.microsoft_watermark(account_id)}
        else:
            folder = ' '.join(str(folder or 'INBOX').split()) or 'INBOX'
            uidvalidity, highest = self.importer.imap_watermark(account_id, folder)
            mark = {'uidvalidity': uidvalidity, 'uid': highest}
        state = self.config(account_id)
        state.update(enabled=True, interval=interval, folder=folder, mark=mark, stopped='',
                     last_run=self.clock(), last_result='Aktiviert. Abgerufen werden nur neue Nachrichten.')
        self._save(account_id, state)
        return state

    def deactivate(self, account_id):
        state = self.config(account_id)
        state.update(enabled=False, last_result='Deaktiviert.')
        self._save(account_id, state)
        return state

    # --- runs --------------------------------------------------------------------------------
    def due(self):
        now = self.clock()
        return [account['id'] for account, state in self.accounts()
                if state['enabled'] and not state['stopped'] and state['mark'] is not None
                and (state['last_run'] is None or now - state['last_run'] >= state['interval'] * 60)]

    def run(self, account_id):
        """One sync run for an active account; returns the updated state with ``imported``."""
        state = self.config(account_id)
        if not state['enabled'] or state['stopped'] or state['mark'] is None:
            raise ValueError('Synchronisation ist für dieses Konto nicht aktiv.')
        state['last_run'] = self.clock()
        imported = 0
        try:
            account = self.importer.accounts.get(account_id)
            if account['provider'] == 'microsoft':
                items = self.importer.new_microsoft_messages(account_id, state['mark']['after'], BATCH_LIMIT)
                new_mark = {'after': max([item['date'] for item in items] + [state['mark']['after']])}
            else:
                mark = state['mark']
                items = self.importer.new_imap_messages(account_id, state['folder'], mark['uidvalidity'],
                                                        mark['uid'], BATCH_LIMIT)
                new_mark = {'uidvalidity': mark['uidvalidity'],
                            'uid': max([int(item['id']) for item in items] + [mark['uid']])}
            fresh = [item for item in items if not item.get('imported')]
            if fresh:
                job_id = self.importer.create_mail_job(self.jobs, account_id, state['folder'], fresh)
                summary = self.jobs.run(job_id)
                imported = summary.get('imported', 0)
                problems = len(fresh) - imported - summary.get('duplicate', 0)
                state['last_result'] = (f'{imported} neue E-Mails importiert' +
                                        (f', {problems} mit Problemen (Auftrag {job_id})' if problems > 0 else '') + '.')
            else:
                state['last_result'] = 'Keine neuen E-Mails.'
            state['mark'] = new_mark
        except MailLoginError as error:
            state['stopped'] = str(error)
            state['last_result'] = 'Gestoppt: ' + str(error)
        except Exception as error:  # e.g. server unreachable: try again at the next interval
            state['last_result'] = 'Fehlgeschlagen, neuer Versuch beim nächsten Intervall: ' + str(error)
        self._save(account_id, state)
        state['imported'] = imported
        return state

    def run_due(self):
        return {account_id: self.run(account_id) for account_id in self.due()}
