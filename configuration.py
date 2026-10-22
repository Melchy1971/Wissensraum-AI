"""Versioned, allow-listed configuration transfer without credentials or content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from importer import MAIL_PROVIDERS
from settings import PROVIDERS
from design import THEMES
from configuration_authority import (ConfigurationAuthority, identity_for_profile,
                                     InterProcessLock, configuration_operation_lock,
                                     normalize_store_identity, recover_pending_authority,
                                     store_identity, _sync_directory)


FORMAT = 'wissensraum-configuration'
VERSION = 1
EXPORT_ERROR = 'Konfiguration konnte nicht sicher exportiert werden.'
AUTHORITY_ERROR = ('Konfigurationsexport benötigt einen aktuellen lokalen Konfigurationsstand; '
                   'Anwendung mit der aktiven Datenbank starten und erneut versuchen.')
IMPORT_ERROR = 'Konfigurationsdatei ist ungültig; es wurde nichts geändert.'
APPLY_ERROR = 'Konfiguration konnte nicht importiert werden; es wurde nichts geändert.'
RECOVERY_ERROR = ('Konfigurationswiederherstellung ist unvollständig; '
                  'das Wiederherstellungsjournal wurde beibehalten.')
MODEL_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}')
EMAIL_PATTERN = re.compile(r'[^\s@]+@[^\s@]+\.[^\s@]+')
HOST_PATTERN = re.compile(r'[A-Za-z0-9.-]{1,253}')
TENANT_PATTERN = re.compile(r'[A-Za-z0-9.-]{1,255}')
SECRET_KEY_PARTS = frozenset({
    'password', 'passwort', 'passwd', 'pwd', 'secret', 'token', 'access_token',
    'refresh_token', 'device_token', 'api_key', 'apikey', 'credential', 'credentials',
    'private_key', 'keyring', 'oauth_token',
})
AI_KEYS = frozenset({'active_provider', 'providers'})
PROVIDER_KEYS = frozenset({'model', 'disabled'})
DATABASE_KEYS = frozenset({'backend', 'host', 'port', 'database', 'username', 'sslmode'})
DESIGN_KEYS = frozenset({'theme'})
ACCOUNT_KEYS = frozenset({'id', 'label', 'provider', 'email', 'username', 'host', 'port',
                          'client_id', 'tenant'})
ROOT_KEYS = frozenset({'format', 'version', 'configuration'})
CONFIGURATION_KEYS = frozenset({'ai', 'database', 'design', 'email_accounts'})


class ConfigurationTransferError(ValueError):
    """A fixed, redacted error safe for presentation."""
    def __init__(self, message, code):
        self.code = code
        super().__init__(message)


def pending_configuration_recovery(base):
    """Check known recovery markers without opening or parsing either file."""
    data = Path(base) / 'data'
    return any(path.exists() for path in (
        data / 'configuration-import-journal.json',
        data / 'authority-resync-pending.json',
    ))


def _exact_dict(value, keys):
    if type(value) is not dict:
        return False
    actual = tuple(value)
    return (all(type(key) is str for key in actual) and len(actual) == len(keys) and
            all(key in keys for key in actual))


def _exact_string(value, maximum, allow_empty=False):
    return (type(value) is str and len(value) <= maximum and
            (allow_empty or bool(value)) and not any(ord(char) < 32 for char in value))


def _check_structure_limits(value):
    stack = [(value, 0)]
    containers = 0
    while stack:
        item, depth = stack.pop()
        if type(item) is str and len(item) > 4096:
            raise ValueError
        if type(item) in (dict, list):
            containers += 1
            if depth >= 20 or containers > 1500 or (type(item) is list and len(item) > 1000):
                raise ValueError
            children = item.items() if type(item) is dict else enumerate(item)
            for key, child in children:
                if type(item) is dict and (type(key) is not str or len(key) > 128):
                    raise ValueError
                stack.append((child, depth + 1))


def _contains_secret_key(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
          for key, child in item.items():
            if type(key) is not str:
                return True
            normalized = key.lower().replace('-', '_')
            if normalized in SECRET_KEY_PARTS or any(
                    normalized.endswith('_' + part) for part in SECRET_KEY_PARTS):
                return True
            stack.append(child)
        elif type(item) is list:
            stack.extend(item)
    return False


def _expected_identity_matches(expected, actual):
    """Require the exact identity established by the non-secret local source."""
    expected = normalize_store_identity(expected)
    actual = normalize_store_identity(actual)
    return expected == actual


class ConfigurationTransfer:
    """Export and transactionally apply the supported non-secret configuration."""

    MAX_IMPORT_BYTES = 1024 * 1024
    MARKER_NAME = '.configuration_import_transaction'

    def __init__(self, store, base, database_settings, design_settings,
                 expected_store_identity=None):
        self.store = store
        self.base = Path(base).resolve()
        self.database_settings = database_settings
        self.design_settings = design_settings
        self.journal_path = self.base / 'data' / 'configuration-import-journal.json'
        self.operation_lock = configuration_operation_lock(self.base)
        self.authority = ConfigurationAuthority(self.base)
        self.authority_lock = InterProcessLock(self.authority.lock_path)
        self.authority_path = self.authority.path
        self.expected_store_identity = (normalize_store_identity(expected_store_identity)
                                        if expected_store_identity is not None else None)

    def synchronize_authority(self):
        if self.store is None:
            raise ValueError('no authenticated store')
        authority = self.authority.synchronize(self.store)
        self._configuration_from_authority(authority)
        return authority

    def export_document(self):
        try:
            return self._export_document()
        except ConfigurationTransferError:
            raise
        except Exception:
            raise ConfigurationTransferError(EXPORT_ERROR, 'CONFIG_EXPORT_FAILED') from None

    def _export_document(self):
        try:
            self.operation_lock.acquire()
        except BaseException as error:
            if isinstance(error, Exception):
                raise ConfigurationTransferError(EXPORT_ERROR, 'CONFIG_EXPORT_BUSY') from None
            raise
        try:
            return self._export_document_owned()
        finally:
            self.operation_lock.release()

    def _export_document_owned(self):
        if self.store is not None:
            try:
                authority = self.synchronize_authority()
                expected = store_identity(self.store)
            except Exception:
                raise ConfigurationTransferError(EXPORT_ERROR, 'CONFIG_EXPORT_FAILED') from None
        else:
            try:
                if self.authority.pending_path.exists():
                    raise ValueError
                authority = self.authority.load()
                expected = self.expected_store_identity
                if expected is None or not _expected_identity_matches(expected, authority['store']):
                    raise ValueError
            except Exception:
                raise ConfigurationTransferError(AUTHORITY_ERROR, 'CONFIG_AUTHORITY_UNAVAILABLE') from None
        stored = authority['settings']
        accounts = authority['email_accounts']
        active = stored.get('active_provider', 'openai')
        if type(active) is not str or active not in PROVIDERS:
            raise ValueError
        providers = {}
        for provider, info in PROVIDERS.items():
            model = stored.get(provider + '.model', info['models'][0])
            disabled = stored.get(provider + '.disabled', '0')
            if (type(model) is not str or type(disabled) is not str or
                    not MODEL_PATTERN.fullmatch(model) or disabled not in ('0', '1')):
                raise ValueError
            providers[provider] = {'model': model, 'disabled': disabled == '1'}
        database = self._database_profile_from_authority(authority)
        document = {
            'format': FORMAT,
            'version': VERSION,
            'configuration': {
                'ai': {'active_provider': active, 'providers': providers},
                'database': database,
                'design': self.design_settings.values(),
                'email_accounts': accounts,
            },
        }
        # Existing stores may predate current validation. Never emit an artifact that import rejects.
        self._validate(document)
        return document

    def _configuration_from_authority(self, authority):
        stored = authority['settings']
        active = stored.get('active_provider', 'openai')
        if type(active) is not str:
            raise ValueError
        providers = {}
        for provider, info in PROVIDERS.items():
            model = stored.get(provider + '.model', info['models'][0])
            disabled = stored.get(provider + '.disabled', '0')
            if type(model) is not str or type(disabled) is not str:
                raise ValueError
            providers[provider] = {
                'model': model,
                'disabled': disabled == '1',
            }
        candidate = {'format': FORMAT, 'version': VERSION, 'configuration': {
            'ai': {'active_provider': active, 'providers': providers},
            'database': self._database_profile_from_authority(authority),
            'design': self.design_settings.values(),
            'email_accounts': authority['email_accounts'],
        }}
        return self._validate(candidate)

    def export_file(self, path):
        try:
            self._write_json_atomic(Path(path), self._export_document())
        except ConfigurationTransferError:
            raise
        except Exception:
            raise ConfigurationTransferError(EXPORT_ERROR, 'CONFIG_EXPORT_FAILED') from None

    def import_file(self, path):
        try:
            with Path(path).open('rb') as source:
                raw = source.read(self.MAX_IMPORT_BYTES + 1)
            if len(raw) > self.MAX_IMPORT_BYTES:
                raise ValueError
            document = json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError,
                RecursionError, MemoryError):
            raise ConfigurationTransferError(IMPORT_ERROR, 'CONFIG_IMPORT_INVALID') from None
        return self.import_document(document)

    def import_document(self, document):
        try:
            validated = self._validate(document)
        except Exception:
            raise ConfigurationTransferError(IMPORT_ERROR, 'CONFIG_IMPORT_INVALID') from None
        try:
            return self._import_validated(validated)
        except ConfigurationTransferError:
            raise
        except Exception:
            raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_FAILED') from None

    def _import_validated(self, validated):
        operation_acquired = False
        authority_acquired = False
        try:
            self.operation_lock.acquire()
            operation_acquired = True
            self.authority_lock.acquire()
            authority_acquired = True
            if self.authority.pending_path.exists():
                raise BlockingIOError('authority recovery is pending')
        except BaseException as error:
            if authority_acquired:
                self.authority_lock.release()
            if operation_acquired:
                self.operation_lock.release()
            if isinstance(error, Exception):
                raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_BUSY') from None
            raise
        try:
            return self._import_validated_owned(validated)
        finally:
            self.authority_lock.release()
            self.operation_lock.release()

    def _import_validated_owned(self, validated):
        try:
            self._recover_pending()
        except Exception:
            raise ConfigurationTransferError(RECOVERY_ERROR, 'CONFIG_IMPORT_RECOVERY_INCOMPLETE') from None
        if self.store is None:
            raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_FAILED')
        try:
            snapshots = self._semantic_snapshots()
        except Exception:
            raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_FAILED') from None
        previous_profile = self._normalized_profile(self.database_settings.persisted_profile())
        origin = store_identity(self.store)
        targets = (self.database_settings.path, self.design_settings.path, self.authority_path)
        transaction_id = uuid.uuid4().hex
        journal = {'format': 'wissensraum-configuration-journal', 'version': 3,
                   'transaction_id': transaction_id, 'phase': 'prepared',
                   'origin_store': origin,
                   'snapshots': snapshots,
                   'configuration': validated}
        try:
            self._write_journal(journal)
        except Exception:
            raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_FAILED') from None
        try:
            with self.store.connect() as db:
                self._apply_database(db, validated, transaction_id)
                self._write_json_atomic(targets[0], validated['database'])
                self._write_json_atomic(targets[1], validated['design'])
                self._write_json_atomic(targets[2], self._authority_document(origin, validated))
            self._verify_applied(validated, transaction_id, origin)
            journal['phase'] = 'committed'
            self._write_journal(journal)
            self._finish_committed(transaction_id)
        except Exception:
            try:
                outcome = self._recover_pending()
            except Exception:
                raise ConfigurationTransferError(RECOVERY_ERROR, 'CONFIG_IMPORT_RECOVERY_INCOMPLETE') from None
            if outcome == 'committed_finalized':
                return {'restart_required': validated['database'] != previous_profile}
            if outcome == 'rollback_verified':
                raise ConfigurationTransferError(APPLY_ERROR, 'CONFIG_IMPORT_FAILED') from None
            raise ConfigurationTransferError(RECOVERY_ERROR, 'CONFIG_IMPORT_RECOVERY_INCOMPLETE') from None
        return {'restart_required': validated['database'] != previous_profile}

    @staticmethod
    def _authority_document(origin, configuration):
        ai = configuration['ai']
        values = {'active_provider': ai['active_provider']}
        for provider, provider_values in ai['providers'].items():
            values[provider + '.model'] = provider_values['model']
            values[provider + '.disabled'] = '1' if provider_values['disabled'] else '0'
        return {'format': 'wissensraum-configuration-authority', 'version': 1,
                'store': origin, 'settings': values,
                'email_accounts': sorted(configuration['email_accounts'],
                                         key=lambda account: (account['label'].lower(), account['id']))}

    def _apply_database(self, db, configuration, transaction_id):
        ai = configuration['ai']
        values = {'active_provider': ai['active_provider'], self.MARKER_NAME: transaction_id}
        for provider, provider_values in ai['providers'].items():
            values[provider + '.model'] = provider_values['model']
            values[provider + '.disabled'] = '1' if provider_values['disabled'] else '0'
        db.executemany('DELETE FROM settings WHERE name=?', ((name,) for name in values))
        db.executemany('INSERT INTO settings(name,value) VALUES(?,?)', values.items())
        wanted = {account['id'] for account in configuration['email_accounts']}
        existing = {row['id'] for row in db.execute('SELECT id FROM email_accounts').fetchall()}
        db.executemany('DELETE FROM email_accounts WHERE id=?', ((identity,) for identity in existing-wanted))
        for account in configuration['email_accounts']:
            db.execute('''INSERT INTO email_accounts
                (id,label,provider,email,username,host,port,client_id,tenant)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,provider=excluded.provider,email=excluded.email,
                username=excluded.username,host=excluded.host,port=excluded.port,
                client_id=excluded.client_id,tenant=excluded.tenant,updated_at=CURRENT_TIMESTAMP''',
                tuple(account[name] for name in
                      ('id','label','provider','email','username','host','port','client_id','tenant')))

    @staticmethod
    def _normalized_profile(profile):
        if not _exact_dict(profile, DATABASE_KEYS):
            raise ValueError
        result = {}
        for name in DATABASE_KEYS:
            if name == 'port':
                continue
            if type(profile[name]) is not str:
                raise ValueError
            result[name] = profile[name]
        port = profile['port']
        if type(port) is str:
            if not port.isdigit():
                raise ValueError
            port = int(port)
        if type(port) is not int:
            raise ValueError
        result['port'] = port
        return result

    def _database_profile_from_authority(self, authority):
        """Use persisted profile data and reject environment-only active PostgreSQL."""
        profile = self._normalized_profile(self.database_settings.persisted_profile())
        identity = normalize_store_identity(authority['store'])
        if identity['backend'] == 'postgresql' and not self.database_settings.path.is_file():
            raise ValueError
        return profile

    def _recover_pending(self):
        operation_acquired = False
        authority_acquired = False
        try:
            self.operation_lock.acquire()
            operation_acquired = True
            self.authority_lock.acquire()
            authority_acquired = True
        except BaseException:
            if authority_acquired:
                self.authority_lock.release()
            if operation_acquired:
                self.operation_lock.release()
            raise
        try:
            return self._recover_pending_owned()
        finally:
            self.authority_lock.release()
            self.operation_lock.release()

    def _recover_pending_owned(self):
        if not self.journal_path.exists():
            return 'no_pending'
        journal = self._read_journal()
        if self.store is None or store_identity(self.store) != journal['origin_store']:
            raise OSError('exact origin store is not open')
        transaction_id = journal['transaction_id']
        marker = self._marker_value()
        committed = journal['phase'] == 'committed' or marker == transaction_id
        if committed:
            configuration = self._validate({'format': FORMAT, 'version': VERSION,
                                            'configuration': journal['configuration']})
            origin = journal['origin_store']
            self._write_json_atomic(self.database_settings.path, configuration['database'])
            self._write_json_atomic(self.design_settings.path, configuration['design'])
            self._write_json_atomic(self.authority_path,
                                    self._authority_document(origin, configuration))
            self._verify_applied(configuration,
                                 transaction_id if marker == transaction_id else None,
                                 origin)
            journal['phase'] = 'committed'
            self._write_journal(journal)
            self._finish_committed(transaction_id)
            return 'committed_finalized'
        snapshots = journal['snapshots']
        targets = {'database': self.database_settings.path,
                   'design': self.design_settings.path,
                   'authority': self.authority_path}
        failed = False
        for name, path in targets.items():
            try:
                self._restore(path, snapshots[name])
            except OSError:
                failed = True
        for name, path in targets.items():
            try:
                if not self._snapshot_matches(path, snapshots[name]):
                    failed = True
            except OSError:
                failed = True
        if failed:
            raise OSError('recovery incomplete')
        self.authority._remove_durable(self.journal_path)
        return 'rollback_verified'

    def _finish_committed(self, transaction_id):
        with self.store.connect() as db:
            db.execute('DELETE FROM settings WHERE name=? AND value=?',
                       (self.MARKER_NAME, transaction_id))
        self.authority._remove_durable(self.journal_path)

    def _marker_value(self):
        with self.store.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?',
                             (self.MARKER_NAME,)).fetchone()
        return row['value'] if row else None

    def _verify_applied(self, configuration, transaction_id, origin):
        expected_authority = self._authority_document(origin, configuration)
        if (self.database_settings.path.read_bytes() != self._json_bytes(configuration['database']) or
                self.design_settings.path.read_bytes() != self._json_bytes(configuration['design']) or
                self.authority_path.read_bytes() != self._json_bytes(expected_authority)):
            raise OSError('file verification failed')
        with self.store.connect() as db:
            marker = db.execute('SELECT value FROM settings WHERE name=?',
                                (self.MARKER_NAME,)).fetchone()
            stored = {row['name']: row['value'] for row in
                      db.execute('SELECT name,value FROM settings').fetchall()}
            accounts = [dict(row) for row in db.execute(
                '''SELECT id,label,provider,email,username,host,port,client_id,tenant
                   FROM email_accounts ORDER BY lower(label),id''').fetchall()]
        expected_accounts = expected_authority['email_accounts']
        expected_settings = expected_authority['settings']
        if (transaction_id is not None and (not marker or marker['value'] != transaction_id)):
            raise OSError('database marker verification failed')
        if any(stored.get(name) != value for name, value in expected_settings.items()):
            raise OSError('database settings verification failed')
        if accounts != expected_accounts:
            raise OSError('database account verification failed')

    def _write_journal(self, journal):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_name(
            self.journal_path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as output:
                output.write(self._json_bytes(journal))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal_path)
            _sync_directory(self.journal_path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_journal(self):
        with self.journal_path.open('rb') as source:
            raw = source.read(self.MAX_IMPORT_BYTES + 1)
        if len(raw) > self.MAX_IMPORT_BYTES:
            raise ValueError
        journal = json.loads(raw.decode('utf-8'))
        journal_keys = frozenset({'format', 'version', 'transaction_id', 'phase',
                                  'origin_store', 'snapshots', 'configuration'})
        if not _exact_dict(journal, journal_keys):
            raise ValueError
        if (type(journal['format']) is not str or type(journal['version']) is not int or
                type(journal['transaction_id']) is not str or type(journal['phase']) is not str or
                not _exact_dict(journal['snapshots'],
                                frozenset({'database', 'design', 'authority'})) or
                type(journal['configuration']) is not dict):
            raise ValueError
        if (journal['format'] != 'wissensraum-configuration-journal' or
                journal['version'] != 3 or journal['phase'] not in ('prepared', 'committed') or
                not re.fullmatch(r'[0-9a-f]{32}', journal['transaction_id'])):
            raise ValueError
        journal['origin_store'] = normalize_store_identity(journal['origin_store'])
        self._validate_semantic_snapshots(journal['snapshots'])
        return journal

    @staticmethod
    def _validate(document):
        try:
            _check_structure_limits(document)
            if _contains_secret_key(document) or not _exact_dict(document, ROOT_KEYS):
                raise ValueError
            if type(document['format']) is not str or document['format'] != FORMAT:
                raise ValueError
            if type(document['version']) is not int or document['version'] != VERSION:
                raise ValueError
            configuration = document['configuration']
            if not _exact_dict(configuration, CONFIGURATION_KEYS):
                raise ValueError

            ai = configuration['ai']
            if not _exact_dict(ai, AI_KEYS) or type(ai['active_provider']) is not str:
                raise ValueError
            if ai['active_provider'] not in PROVIDERS or not _exact_dict(
                    ai['providers'], frozenset(PROVIDERS)):
                raise ValueError
            for provider in PROVIDERS:
                values = ai['providers'][provider]
                if not _exact_dict(values, PROVIDER_KEYS):
                    raise ValueError
                if type(values['model']) is not str or not MODEL_PATTERN.fullmatch(values['model']):
                    raise ValueError
                if type(values['disabled']) is not bool:
                    raise ValueError

            database = configuration['database']
            if not _exact_dict(database, DATABASE_KEYS):
                raise ValueError
            if type(database['backend']) is not str or database['backend'] not in ('sqlite', 'postgresql'):
                raise ValueError
            if type(database['port']) is not int or not 1 <= database['port'] <= 65535:
                raise ValueError
            for field, maximum in (('host', 253), ('database', 128), ('username', 128), ('sslmode', 20)):
                if not _exact_string(database[field], maximum, allow_empty=field != 'sslmode'):
                    raise ValueError
            if database['host'] and not HOST_PATTERN.fullmatch(database['host']):
                raise ValueError
            allowed_ssl = {'require', 'verify-ca', 'verify-full'}
            if database['host'] in ('localhost', '127.0.0.1'):
                allowed_ssl.add('disable')
            if database['sslmode'] not in allowed_ssl:
                raise ValueError
            if database['backend'] == 'postgresql' and not all(
                    database[field] for field in ('host', 'database', 'username')):
                raise ValueError

            design = configuration['design']
            if not _exact_dict(design, DESIGN_KEYS) or type(design['theme']) is not str or design['theme'] not in THEMES:
                raise ValueError

            accounts = configuration['email_accounts']
            if type(accounts) is not list or len(accounts) > 1000:
                raise ValueError
            identities = set()
            addresses = set()
            for account in accounts:
                if not _exact_dict(account, ACCOUNT_KEYS):
                    raise ValueError
                for field, maximum, empty in (
                    ('id', 128, False), ('label', 120, False), ('provider', 20, False),
                    ('email', 320, False), ('username', 320, True), ('host', 253, True),
                    ('client_id', 36, True), ('tenant', 255, False),
                ):
                    if not _exact_string(account[field], maximum, allow_empty=empty):
                        raise ValueError
                if account['provider'] not in MAIL_PROVIDERS or not EMAIL_PATTERN.fullmatch(account['email']):
                    raise ValueError
                if type(account['port']) is not int or not 0 <= account['port'] <= 65535:
                    raise ValueError
                if account['id'] in identities:
                    raise ValueError
                identity = (account['provider'], account['email'].lower())
                if identity in addresses:
                    raise ValueError
                identities.add(account['id']); addresses.add(identity)
                if account['provider'] == 'microsoft':
                    try:
                        if str(uuid.UUID(account['client_id'])) != account['client_id']:
                            raise ValueError
                    except (ValueError, AttributeError):
                        raise ValueError
                    if (account['host'] or account['port'] != 0 or account['username'] != account['email'] or
                            not TENANT_PATTERN.fullmatch(account['tenant'])):
                        raise ValueError
                elif (not HOST_PATTERN.fullmatch(account['host']) or
                      not 1 <= account['port'] <= 65535 or not account['username']):
                    raise ValueError
            return configuration
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError from None

    def _semantic_snapshots(self):
        snapshots = {
            'database': self._read_semantic_snapshot(self.database_settings.path),
            'design': self._read_semantic_snapshot(self.design_settings.path),
            'authority': self._read_semantic_snapshot(self.authority_path),
        }
        self._validate_semantic_snapshots(snapshots)
        return snapshots

    def _read_semantic_snapshot(self, path):
        try:
            with path.open('rb') as source:
                raw = source.read(self.MAX_IMPORT_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > self.MAX_IMPORT_BYTES:
            raise ValueError
        return json.loads(raw.decode('utf-8'))

    @staticmethod
    def _validate_semantic_snapshots(snapshots):
        if (type(snapshots) is not dict or
                frozenset(snapshots) != frozenset({'database', 'design', 'authority'})):
            raise ValueError
        database = snapshots['database']
        if database is not None:
            if (not _exact_dict(database, DATABASE_KEYS) or
                    type(database['backend']) is not str or
                    database['backend'] not in ('sqlite', 'postgresql') or
                    type(database['port']) is not int or type(database['port']) is bool or
                    not 1 <= database['port'] <= 65535 or
                    any(type(database[name]) is not str
                        for name in DATABASE_KEYS if name != 'port')):
                raise ValueError
        design = snapshots['design']
        if (design is not None and
                (not _exact_dict(design, DESIGN_KEYS) or
                 type(design['theme']) is not str or design['theme'] not in THEMES)):
            raise ValueError
        authority = snapshots['authority']
        if authority is not None:
            ConfigurationAuthority.validate_document(authority)

    def _snapshot_matches(self, path, snapshot):
        try:
            current = self._read_semantic_snapshot(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return False
        return current == snapshot

    @classmethod
    def _restore(cls, path, snapshot):
        if snapshot is None:
            ConfigurationAuthority(path.parents[1])._remove_durable(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.rollback-' + uuid.uuid4().hex)
        try:
            data = cls._json_bytes(snapshot)
            if path.name in ('database.json', 'design.json'):
                data = data.replace(b'\n', b'\r\n')
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _json_bytes(value):
        return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

    @classmethod
    def _write_json_atomic(cls, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            data = cls._json_bytes(value)
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _open_origin_store(identity, database_settings, _sqlite_path=None):
    """Open only the exact non-secret store identity recorded by the journal."""
    identity = normalize_store_identity(identity)
    if identity['backend'] == 'sqlite':
        path = Path(identity['path'])
        if not path.is_file():
            raise OSError('origin SQLite store is unavailable')
        from core import Store
        return Store(path, initialize=False)
    from core import PostgresStore
    profile, password = database_settings.connection_values(identity)
    return PostgresStore(profile, password, initialize=False)


def recover_pending_configuration(base, database_settings, sqlite_path=None):
    """Resolve a pending import against its recorded origin before profile selection."""
    base = Path(base)
    design_settings = __import__('design').DesignSettings(base)
    reader = ConfigurationTransfer(None, base, database_settings, design_settings)
    try:
        authority_result = 'no_pending'
        if reader.authority.pending_path.exists():
            marker = reader.authority.read_pending()
            authority_store = _open_origin_store(
                marker['store'], database_settings, sqlite_path)
            authority_result = recover_pending_authority(base, authority_store)
        if not reader.journal_path.exists():
            return authority_result
        journal = reader._read_journal()
        store = _open_origin_store(journal['origin_store'], database_settings, sqlite_path)
        transfer = ConfigurationTransfer(store, base, database_settings, design_settings)
        return transfer._recover_pending()
    except Exception:
        raise ConfigurationTransferError(
            RECOVERY_ERROR, 'CONFIG_IMPORT_RECOVERY_INCOMPLETE') from None
