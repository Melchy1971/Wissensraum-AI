"""Backend-independent, non-secret authority for transferable configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid


AUTHORITY_FORMAT = 'wissensraum-configuration-authority'
AUTHORITY_VERSION = 1
AUTHORITY_RECOVERY_ERROR = ('Konfigurationsautorität konnte nicht wiederhergestellt werden; '
                            'die Wiederherstellungsmarkierung wurde beibehalten.')
CONFIGURATION_OPERATION_BUSY = 'Konfiguration wird gerade geändert; bitte erneut versuchen.'
RESYNC_FORMAT = 'wissensraum-authority-resync'
RESYNC_VERSION = 1
TRANSFER_SETTING_NAMES = frozenset({
    'active_provider',
    'openai.model', 'openai.disabled',
    'anthropic.model', 'anthropic.disabled',
    'gemini.model', 'gemini.disabled',
    'ollama.model', 'ollama.disabled',
})
ACCOUNT_COLUMNS = ('id', 'label', 'provider', 'email', 'username', 'host', 'port',
                   'client_id', 'tenant')
POSTGRES_IDENTITY_KEYS = frozenset({'backend', 'host', 'port', 'database', 'username', 'sslmode'})
SQLITE_INSTANCE_SETTING = '.configuration_store_instance'
SQLITE_INSTANCE_PATTERN = re.compile(r'[0-9a-f]{32}')
LOCAL_IDENTITY_FORMAT = 'wissensraum-local-store-identity'
LOCAL_IDENTITY_VERSION = 1


def _exact_dict_keys(value, expected):
    if type(value) is not dict:
        return False
    keys = tuple(value)
    return (all(type(key) is str for key in keys) and len(keys) == len(expected) and
            all(key in expected for key in keys))


class InterProcessLock:
    """A bounded advisory lock released automatically when a process exits."""

    def __init__(self, path, timeout=0.25):
        self.path = Path(path).resolve()
        self.timeout = timeout
        self._file = None
        self._depth = 0
        self._owner = None

    def acquire(self):
        owner = threading.get_ident()
        if self._file is not None and self._owner == owner:
            self._depth += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open('a+b')
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b'0')
                lock_file.flush()
                os.fsync(lock_file.fileno())
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    lock_file.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._file = lock_file
                    self._depth = 1
                    self._owner = owner
                    return self
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise BlockingIOError('configuration operation is busy') from None
                    time.sleep(0.01)
        except BaseException:
            lock_file.close()
            raise

    def release(self):
        if self._depth > 1:
            self._depth -= 1
            return
        lock_file, self._file = self._file, None
        self._depth = 0
        self._owner = None
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            lock_file.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def configuration_operation_lock(base, timeout=0.25):
    """Return the canonical bounded lock for configuration generations."""
    return InterProcessLock(
        Path(base).resolve() / 'data' / 'configuration-import.lock', timeout=timeout)


class _PreparedAuthority(dict):
    def __init__(self, document, operation_id, lock):
        super().__init__(document)
        self.operation_id = operation_id
        self.lock = lock

    def release(self):
        lock, self.lock = self.lock, None
        if lock is not None:
            lock.release()

    def __del__(self):
        self.release()


def _sync_directory(path):
    """Make a directory entry update durable where the platform supports it."""
    path = Path(path)
    if os.name != 'nt':
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    # Windows has no Python directory-fsync API. Try FlushFileBuffers on a
    # directory handle; ERROR_INVALID_FUNCTION/INVALID_HANDLE means the
    # filesystem does not support it, after the strongest available attempt.
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateFileW(
        str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x02000000, None)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), 'directory handle could not be opened')
    try:
        if not kernel32.FlushFileBuffers(handle):
            error = ctypes.get_last_error()
            # Directory flushing is unsupported on common Windows filesystems.
            if error not in (1, 5, 6, 87):
                raise OSError(error, 'directory flush failed')
    finally:
        kernel32.CloseHandle(handle)


def _linux_birth_time_ns(path):
    """Return the Linux statx creation time, or None if the filesystem lacks it."""
    # ponytail: statx via libc because os.statx only exists from Python 3.15;
    # replace with os.statx once 3.15 is the minimum version.
    import ctypes
    import struct
    try:
        statx = ctypes.CDLL(None, use_errno=True).statx
    except (OSError, AttributeError):
        return None
    statx_btime = 0x800
    buffer = ctypes.create_string_buffer(256)
    if statx(-100, os.fsencode(path), 0, statx_btime, buffer) != 0:
        return None
    if not struct.unpack_from('=I', buffer, 0)[0] & statx_btime:
        return None
    seconds, nanoseconds = struct.unpack_from('=qI', buffer, 80)
    return seconds * 1_000_000_000 + nanoseconds


def _filesystem_binding(path):
    status = Path(path).stat()
    binding = {'device': str(status.st_dev), 'inode': str(status.st_ino)}
    if os.name == 'nt':
        binding['created_ns'] = str(status.st_ctime_ns)
    elif sys.platform.startswith('linux'):
        # Inode numbers are reused after unlink; creation time detects a
        # replaced database file at the same path without opening SQL.
        created_ns = _linux_birth_time_ns(path)
        if created_ns is not None:
            binding['created_ns'] = str(created_ns)
    return binding


def store_identity(store, db=None):
    """Return a validated non-secret identity for the exact opened SQL store."""
    backend = getattr(store, 'backend', None)
    if type(backend) is not str:
        raise ValueError('unknown store identity')
    if backend == 'sqlite':
        path = getattr(store, 'path', None)
        if path is None:
            raise ValueError('sqlite identity missing')
        def ensure_instance(connection):
            row = connection.execute('SELECT value FROM settings WHERE name=?',
                                     (SQLITE_INSTANCE_SETTING,)).fetchone()
            instance = row['value'] if row else uuid.uuid4().hex
            if type(instance) is not str or not SQLITE_INSTANCE_PATTERN.fullmatch(instance):
                raise ValueError('invalid sqlite instance identity')
            if row is None:
                connection.execute('INSERT INTO settings(name,value) VALUES(?,?)',
                                   (SQLITE_INSTANCE_SETTING, instance))
            return instance
        if db is None:
            with store.connect() as connection:
                instance_id = ensure_instance(connection)
        else:
            instance_id = ensure_instance(db)
        return {'backend': 'sqlite', 'path': str(Path(path).resolve()),
                'instance_id': instance_id}
    if backend == 'postgresql':
        profile = getattr(store, 'profile', None)
        if type(profile) is not dict or not all(type(key) is str for key in profile):
            raise ValueError('postgres identity missing')
        return normalize_store_identity({'backend': 'postgresql', **profile})
    raise ValueError('unknown store identity')


def identity_for_profile(profile, sqlite_path=None, base=None):
    """Resolve an export identity using only persisted, non-secret local state."""
    if type(profile) is not dict or not all(type(key) is str for key in profile):
        raise ValueError('invalid store profile')
    backend = profile.get('backend')
    if type(backend) is not str:
        raise ValueError('invalid store profile')
    if backend == 'sqlite':
        if base is None:
            raise ValueError('local identity source required')
        return ConfigurationAuthority(base).load_local_identity(sqlite_path)
    return normalize_store_identity(profile)


def normalize_store_identity(value):
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise ValueError('invalid store identity')
    backend = value.get('backend')
    if type(backend) is not str or backend not in ('sqlite', 'postgresql'):
        raise ValueError('invalid store identity')
    if backend == 'sqlite':
        keys = tuple(value)
        if not ((len(keys) == 2 and all(key in {'backend', 'path'} for key in keys)) or
                (len(keys) == 3 and all(
                    key in {'backend', 'path', 'instance_id'} for key in keys))):
            raise ValueError('invalid sqlite identity')
        path = value['path']
        if type(path) is not str or not path or len(path) > 4096 or any(ord(c) < 32 for c in path):
            raise ValueError('invalid sqlite identity')
        resolved = str(Path(path).resolve())
        if resolved != path:
            raise ValueError('sqlite identity must be absolute and normalized')
        result = {'backend': 'sqlite', 'path': resolved}
        if 'instance_id' in value:
            instance_id = value['instance_id']
            if type(instance_id) is not str or not SQLITE_INSTANCE_PATTERN.fullmatch(instance_id):
                raise ValueError('invalid sqlite identity')
            result['instance_id'] = instance_id
        return result
    if not _exact_dict_keys(value, POSTGRES_IDENTITY_KEYS):
        raise ValueError('invalid postgres identity')
    result = {'backend': 'postgresql'}
    for name, maximum, empty in (
            ('host', 253, False), ('database', 128, False), ('username', 128, False),
            ('sslmode', 20, False)):
        item = value[name]
        if type(item) is not str or (not empty and not item) or len(item) > maximum or any(ord(c) < 32 for c in item):
            raise ValueError('invalid postgres identity')
        result[name] = item
    port = value['port']
    if type(port) is str and port.isdigit():
        port = int(port)
    if type(port) is not int or type(port) is bool or not 1 <= port <= 65535:
        raise ValueError('invalid postgres identity')
    result['port'] = port
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', result['host']):
        raise ValueError('invalid postgres identity')
    allowed_ssl = {'require', 'verify-ca', 'verify-full'}
    if result['host'] in ('localhost', '127.0.0.1'):
        allowed_ssl.add('disable')
    if result['sslmode'] not in allowed_ssl:
        raise ValueError('invalid postgres identity')
    return result


class ConfigurationAuthority:
    """Atomic local mirror of transferable settings for secret-free export."""

    MAX_BYTES = 1024 * 1024

    def __init__(self, base):
        data = Path(base).resolve() / 'data'
        self.path = data / 'configuration-authority.json'
        self.pending_path = data / 'authority-resync-pending.json'
        self.local_identity_path = data / 'configuration-store-identity.json'
        self.lock_path = data / 'configuration-authority.lock'

    def snapshot(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def restore(self, snapshot):
        if snapshot is None:
            self._remove_durable(self.path)
        else:
            self._write_bytes(snapshot)

    def synchronize(self, store, db=None):
        """Prepare a mirror while retaining its marker until the SQL commit."""
        if db is None:
            prepared = None
            try:
                # Establish SQLite's instance identifier in its own committed
                # transaction before any pending marker can refer to it.
                store_identity(store)
                with store.connect() as connection:
                    prepared = self.synchronize(store, connection)
                return self.finalize_synchronize(store, prepared)
            except Exception:
                self.abort_synchronize(prepared)
                raise ValueError(AUTHORITY_RECOVERY_ERROR) from None
            except BaseException:
                self.abort_synchronize(prepared)
                raise
        lock = InterProcessLock(self.lock_path)
        try:
            lock.acquire()
            identity = store_identity(store, db)
            if self.pending_path.exists():
                raise ValueError(AUTHORITY_RECOVERY_ERROR)
            operation_id = uuid.uuid4().hex
            marker = {'format': RESYNC_FORMAT, 'version': RESYNC_VERSION,
                      'operation_id': operation_id, 'store': identity}
            document = self._document_from_store(store, identity, db)
            self._write_json(self.pending_path, marker)
            self.write(document)
            if self.load() != document:
                raise OSError('authority verification failed')
            return _PreparedAuthority(document, operation_id, lock)
        except Exception:
            lock.release()
            raise ValueError(AUTHORITY_RECOVERY_ERROR) from None
        except BaseException:
            lock.release()
            raise

    def finalize_synchronize(self, store, prepared=None):
        """Verify committed SQL through a fresh connection, then clear the marker."""
        try:
            if type(prepared) is not _PreparedAuthority or prepared.lock is None:
                raise ValueError('operation ownership missing')
            marker = self.read_pending()
            if marker['operation_id'] != prepared.operation_id:
                raise ValueError('operation mismatch')
            identity = store_identity(store)
            if identity != marker['store']:
                raise ValueError('store mismatch')
            committed = self._document_from_store(store, identity)
            if prepared is not None and committed != prepared:
                raise OSError('SQL commit verification failed')
            if self.load() != committed:
                raise OSError('authority verification failed')
            self._write_local_identity(identity, store)
            if self.read_pending()['operation_id'] != prepared.operation_id:
                raise ValueError('operation changed')
            self._remove_durable(self.pending_path)
            return committed
        except Exception:
            raise ValueError(AUTHORITY_RECOVERY_ERROR) from None
        finally:
            self.abort_synchronize(prepared)

    @staticmethod
    def abort_synchronize(prepared):
        if type(prepared) is _PreparedAuthority:
            prepared.release()

    def _write_local_identity(self, identity, store):
        value = {'format': LOCAL_IDENTITY_FORMAT, 'version': LOCAL_IDENTITY_VERSION,
                 'store': identity, 'binding': None}
        if identity['backend'] == 'sqlite':
            value['binding'] = _filesystem_binding(store.path)
        self._write_json(self.local_identity_path, value)

    def load_local_identity(self, sqlite_path=None):
        with self.local_identity_path.open('rb') as source:
            raw = source.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError('local identity too large')
        value = json.loads(raw.decode('utf-8'))
        if not _exact_dict_keys(
                value, frozenset({'format', 'version', 'store', 'binding'})):
            raise ValueError('invalid local identity')
        if (type(value['format']) is not str or type(value['version']) is not int or
                value['format'] != LOCAL_IDENTITY_FORMAT or
                value['version'] != LOCAL_IDENTITY_VERSION):
            raise ValueError('invalid local identity')
        identity = normalize_store_identity(value['store'])
        path = Path(identity['path'] if sqlite_path is None else sqlite_path).resolve()
        binding = value['binding']
        expected_binding = _filesystem_binding(path)
        if (identity['backend'] != 'sqlite' or identity['path'] != str(path) or
                not _exact_dict_keys(binding, frozenset(expected_binding)) or
                any(type(item) is not str for item in binding.values()) or
                binding != expected_binding):
            raise ValueError('local SQLite identity mismatch')
        return identity

    def _document_from_store(self, store, identity, db=None):
        if db is not None:
            return self._document(identity, db)
        with store.connect() as connection:
            return self._document(identity, connection)

    def read_pending(self):
        """Return a strictly validated non-secret resynchronization marker."""
        with self.pending_path.open('rb') as source:
            raw = source.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError('pending marker too large')
        value = json.loads(raw.decode('utf-8'))
        if type(value) is not dict or not all(type(key) is str for key in value):
            raise ValueError('invalid pending marker')
        keys = tuple(value)
        valid_keys = ((len(keys) == 3 and all(
            key in {'format', 'version', 'store'} for key in keys)) or
            (len(keys) == 4 and all(
                key in {'format', 'version', 'operation_id', 'store'} for key in keys)))
        if (not valid_keys or type(value['format']) is not str or
                type(value['version']) is not int):
            raise ValueError('invalid pending marker')
        if (value['format'] != RESYNC_FORMAT or value['version'] != RESYNC_VERSION):
            raise ValueError('invalid pending marker')
        if 'operation_id' in value:
            if (type(value['operation_id']) is not str or
                    not re.fullmatch(r'[0-9a-f]{32}', value['operation_id'])):
                raise ValueError('invalid pending marker')
        else:
            value['operation_id'] = None
        value['store'] = normalize_store_identity(value['store'])
        return value

    @staticmethod
    def _document(identity, db):
        settings = {}
        for name in sorted(TRANSFER_SETTING_NAMES):
            row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
            if row is not None:
                settings[name] = row['value']
        accounts = [dict(row) for row in db.execute(
            '''SELECT id,label,provider,email,username,host,port,client_id,tenant
               FROM email_accounts ORDER BY lower(label),id''').fetchall()]
        return {'format': AUTHORITY_FORMAT, 'version': AUTHORITY_VERSION,
                'store': identity, 'settings': settings, 'email_accounts': accounts}

    def load(self):
        with self.path.open('rb') as source:
            raw = source.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError('authority too large')
        value = json.loads(raw.decode('utf-8'))
        return self.validate_document(value)

    @staticmethod
    def validate_document(value):
        """Validate and normalize a semantic authority snapshot."""
        expected = frozenset({'format', 'version', 'store', 'settings', 'email_accounts'})
        if not _exact_dict_keys(value, expected):
            raise ValueError('invalid authority')
        if (type(value['format']) is not str or type(value['version']) is not int or
                type(value['settings']) is not dict or
                type(value['email_accounts']) is not list):
            raise ValueError('invalid authority')
        settings = value['settings']
        if (not all(type(name) is str for name in settings) or
                any(type(item) is not str for item in settings.values()) or
                len(value['email_accounts']) > 1000):
            raise ValueError('invalid authority')
        if (value['format'] != AUTHORITY_FORMAT or value['version'] != AUTHORITY_VERSION or
                not all(name in TRANSFER_SETTING_NAMES for name in settings)):
            raise ValueError('invalid authority')
        value['store'] = normalize_store_identity(value['store'])
        for account in value['email_accounts']:
            if (not _exact_dict_keys(account, frozenset(ACCOUNT_COLUMNS)) or
                    type(account['port']) is not int or type(account['port']) is bool or
                    any(type(account[name]) is not str for name in ACCOUNT_COLUMNS if name != 'port')):
                raise ValueError('invalid authority account')
        return value

    def write(self, value):
        self._write_json(self.path, value)

    def _write_json(self, path, value):
        data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        self._write_bytes(data, path)

    def _write_bytes(self, data, path=None):
        path = self.path if path is None else Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_durable(self, path):
        path = Path(path)
        try:
            snapshot = path.read_bytes()
        except FileNotFoundError:
            return
        path.unlink()
        try:
            _sync_directory(path.parent)
        except BaseException:
            try:
                self._write_bytes(snapshot, path)
            finally:
                raise


def recover_pending_authority(base, store):
    """Recover authority only from the exact store recorded by the marker."""
    authority = ConfigurationAuthority(base)
    lock = InterProcessLock(authority.lock_path)
    try:
        lock.acquire()
        if not authority.pending_path.exists():
            return 'no_pending'
        marker = authority.read_pending()
        identity = store_identity(store)
        if identity != marker['store']:
            raise ValueError('store mismatch')
        document = authority._document_from_store(store, identity)
        authority.write(document)
        if authority.load() != document:
            raise OSError('authority verification failed')
        authority._write_local_identity(identity, store)
        if authority.read_pending()['operation_id'] != marker['operation_id']:
            raise ValueError('operation changed')
        authority._remove_durable(authority.pending_path)
        return 'resynchronized'
    except Exception:
        raise ValueError(AUTHORITY_RECOVERY_ERROR) from None
    finally:
        lock.release()
