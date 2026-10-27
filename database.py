"""Local database profile and secure PostgreSQL connection handling."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
from contextlib import contextmanager

from configuration_authority import configuration_operation_lock, _sync_directory


DATABASE_OPERATION_BUSY = 'Datenbankkonfiguration wird gerade geändert; bitte erneut versuchen.'
DATABASE_PERSISTENCE_ERROR = 'Datenbankkonfiguration konnte nicht gespeichert werden.'
DATABASE_PERSISTENCE_FATAL_ERROR = ('Datenbankkonfiguration konnte nicht sicher '
                                    'wiederhergestellt werden; Zustand prüfen.')


def resolve_sqlite_path(base,environ=None,explicit=None):
    """Resolve the SQLite target without changing backend configuration."""
    if explicit is not None:
        return Path(explicit)
    environ=os.environ if environ is None else environ
    return Path(environ.get('WISSEN_DB',Path(base)/'data'/'wissen.db'))


def _probe_missing_sqlite_target(path):
    """Exercise target-name and directory creation under an owned sibling."""
    ancestor=path.parent
    missing_directories=[]
    while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
        missing_directories.append(ancestor)
        ancestor=ancestor.parent
    if not ancestor.is_dir():
        raise ValueError('SQLite-Zielordner ist nicht verwendbar.')

    probe_root=ancestor/('.wissensraum-sqlite-probe-'+uuid.uuid4().hex)
    owned_directories=[]
    probe_file=None
    descriptor=None
    cleanup_failed=False
    try:
        probe_root.mkdir()
        owned_directories.append(probe_root)
        probe_parent=probe_root
        for directory in reversed(missing_directories):
            probe_parent=probe_parent/directory.name
            probe_parent.mkdir()
            owned_directories.append(probe_parent)
        probe_file=probe_parent/path.name
        descriptor=os.open(probe_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        os.write(descriptor,b'probe')
    except OSError:
        raise ValueError('SQLite-Zielordner ist nicht beschreibbar.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe_file is not None and descriptor is not None:
            try: probe_file.unlink()
            except OSError: cleanup_failed=True
        for directory in reversed(owned_directories):
            try: directory.rmdir()
            except OSError: cleanup_failed=True
    if cleanup_failed:
        raise ValueError('SQLite-Zielprüfung konnte nicht rückstandsfrei abgeschlossen werden.')
    if os.path.lexists(path):
        raise ValueError('SQLite-Zielpfad hat sich während der Prüfung geändert.')


class DatabaseSettings:
    secret_name = 'database:postgresql'

    def __init__(self,base,vault,environ=None):
        self.base=Path(base)
        self.vault=vault
        self.environ=os.environ if environ is None else environ
        self.path=self.base/'data'/'database.json'

    def _file_config(self):
        try:
            value=json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError,UnicodeError,json.JSONDecodeError,RecursionError):
            raise ValueError('Datenbankkonfiguration ist nicht lesbar; data/database.json prüfen.') from None
        if type(value) is not dict or not all(type(key) is str for key in value):
            raise ValueError('Datenbankkonfiguration ist ungültig; data/database.json prüfen.')
        return value

    def profile(self):
        saved=self._file_config()
        if 'backend' in saved:
            value=saved['backend']
            if type(value) is not str or not value.strip():
                raise ValueError('Datenbankkonfiguration enthält eine ungültige Datenquelle; data/database.json prüfen.')
            backend=value.strip().lower()
        else:
            if 'DB_BACKEND' not in self.environ:
                backend='sqlite'
            else:
                value=self.environ['DB_BACKEND']
                if type(value) is not str or not value.strip():
                    raise ValueError('Datenbankkonfiguration enthält eine ungültige Datenquelle; DB_BACKEND prüfen.')
                backend=value.strip().lower()
        if backend not in ('sqlite','postgresql'):
            raise ValueError('Datenbankkonfiguration enthält eine unbekannte Datenquelle; data/database.json prüfen.')

        def resolved(field,environment,default,allow_integer=False):
            value=saved[field] if field in saved else self.environ.get(environment,default)
            valid=type(value) is str or (allow_integer and type(value) is int)
            if not valid:
                raise ValueError('Datenbankkonfiguration enthält einen ungültigen PostgreSQL-Wert; data/database.json prüfen.')
            return str(value)

        return {
            'backend':backend,
            'host':resolved('host','DB_HOST',''),
            'port':resolved('port','DB_PORT','5432',allow_integer=True),
            'database':resolved('database','DB_DATABASE',''),
            'username':resolved('username','DB_USERNAME',''),
            'sslmode':resolved('sslmode','DB_SSLMODE','require'),
        }

    def persisted_profile(self):
        """Return validated on-disk values without DB_* environment fallbacks."""
        saved=self._file_config()
        backend=saved.get('backend','sqlite')
        if type(backend) is not str or not backend.strip():
            raise ValueError('Datenbankkonfiguration enthält eine ungültige Datenquelle; data/database.json prüfen.')
        backend=backend.strip().lower()
        if backend not in ('sqlite','postgresql'):
            raise ValueError('Datenbankkonfiguration enthält eine unbekannte Datenquelle; data/database.json prüfen.')
        defaults={'host':'','port':'5432','database':'','username':'','sslmode':'require'}
        result={'backend':backend}
        for field,default in defaults.items():
            value=saved.get(field,default)
            if not (type(value) is str or (field=='port' and type(value) is int)):
                raise ValueError('Datenbankkonfiguration enthält einen ungültigen PostgreSQL-Wert; data/database.json prüfen.')
            result[field]=str(value)
        return result

    def password(self):
        value=self.vault.get(self.secret_name)
        if value:
            return value,'Betriebssystem-Schlüsselspeicher'
        if 'DB_PASSWORD' not in self.environ:
            return '','Nicht hinterlegt'
        value=self.environ['DB_PASSWORD']
        if type(value) is str and not value:
            return '','Nicht hinterlegt'
        return value,'Umgebungsvariable'

    @staticmethod
    def validate(values,password):
        if type(values) is not dict or not all(type(key) is str for key in values):
            raise ValueError('Ungültiger PostgreSQL-Verbindungswert.')
        for field in ('host','database','username','sslmode'):
            if field in values and type(values[field]) is not str:
                raise ValueError('Ungültiger PostgreSQL-Verbindungswert.')
        host=values.get('host','').strip()
        database=values.get('database','').strip()
        username=values.get('username','').strip()
        sslmode=values.get('sslmode','require').strip()
        raw_port=values.get('port',5432)
        if type(raw_port) not in (str,int):
            raise ValueError('Gültigen PostgreSQL-Port eingeben.')
        try:
            port=int(raw_port)
        except (TypeError,ValueError):
            raise ValueError('Gültigen PostgreSQL-Port eingeben.') from None
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host):
            raise ValueError('Gültigen PostgreSQL-Servernamen oder eine IP-Adresse eingeben.')
        if not 1 <= port <= 65535:
            raise ValueError('PostgreSQL-Port muss zwischen 1 und 65535 liegen.')
        for label,value in (('Datenbankname',database),('Benutzername',username)):
            if not value or len(value)>128 or any(ord(c)<32 for c in value):
                raise ValueError(label+' vollständig und ohne Steuerzeichen eingeben.')
        allowed_ssl=('require','verify-ca','verify-full')+(("disable",) if host in ('127.0.0.1','localhost') else ())
        if sslmode not in allowed_ssl:
            raise ValueError('TLS-Modus require, verify-ca oder verify-full auswählen.')
        if type(password) is not str or not password or len(password)>4096 or any(ord(c)<32 for c in password):
            raise ValueError('PostgreSQL-Kennwort eingeben.')
        return {'host':host,'port':port,'database':database,'username':username,'sslmode':sslmode},password

    def connection_values(self,values=None,password=None):
        values=self.profile() if values is None else values
        if password is None:
            password=self.password()[0]
        profile,password=self.validate(values,password)
        return profile,password

    @staticmethod
    def _connect(profile,password,connector=None):
        if connector is None:
            try:
                import psycopg
            except ImportError:
                raise ValueError('PostgreSQL-Unterstützung fehlt. Abhängigkeiten erneut installieren.') from None
            connector=psycopg.connect
        try:
            return connector(host=profile['host'],port=profile['port'],dbname=profile['database'],
                             user=profile['username'],password=password,sslmode=profile['sslmode'],
                             connect_timeout=10,application_name='Wissensraum-AI')
        except Exception:
            raise ValueError('PostgreSQL-Verbindung fehlgeschlagen. Server, Port, Datenbank, Benutzer, Kennwort, TLS und Firewall prüfen.') from None

    def test(self,values=None,password=None,connector=None):
        profile,password=self.connection_values(values,password)
        connection=self._connect(profile,password,connector)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_database(), current_user, version()')
                row=cursor.fetchone()
            return {'database':row[0],'username':row[1],'version':str(row[2]).split(',',1)[0]}
        except Exception:
            raise ValueError('PostgreSQL wurde erreicht, der Verbindungstest konnte aber nicht ausgeführt werden.') from None
        finally:
            connection.close()

    @contextmanager
    def _public_mutation(self):
        lock=configuration_operation_lock(self.base)
        try:
            lock.acquire()
        except Exception:
            raise ValueError(DATABASE_OPERATION_BUSY) from None
        try:
            yield
        finally:
            lock.release()

    def _write_owned(self,profile):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.tmp-'+uuid.uuid4().hex)
        try:
            text=json.dumps(profile,ensure_ascii=False,indent=2)+'\n'
            data=text.replace('\n',os.linesep).encode('utf-8')
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,self.path)
            _sync_directory(self.path.parent)
        finally:
            try: temporary.unlink()
            except FileNotFoundError: pass

    def _write(self,profile):
        with configuration_operation_lock(self.base):
            self._write_owned(profile)

    def _save_owned(self,values,password=None,backend=None):
        previous_vault_password=self.vault.get(self.secret_name)
        current_password=self.password()[0]
        profile,password=self.connection_values(values,current_password if password is None else password)
        selected_backend=self.profile()['backend'] if backend is None else backend
        if type(selected_backend) is not str or selected_backend not in ('sqlite','postgresql'):
            raise ValueError('Datenbankkonfiguration enthält eine unbekannte Datenquelle; data/database.json prüfen.')
        profile['backend']=selected_backend
        password_changed=password != current_password
        if password_changed:
            self.vault.set(self.secret_name,password)
        try:
            self._write_owned(profile)
        except BaseException as error:
            if password_changed:
                try:
                    if previous_vault_password is None:
                        self.vault.delete(self.secret_name)
                    else:
                        self.vault.set(self.secret_name,previous_vault_password)
                    if self.vault.get(self.secret_name) != previous_vault_password:
                        raise RuntimeError('vault rollback verification failed')
                except BaseException:
                    raise ValueError(DATABASE_PERSISTENCE_FATAL_ERROR) from None
            if isinstance(error,Exception):
                raise ValueError(DATABASE_PERSISTENCE_ERROR) from None
            raise
        return profile

    def save(self,values,password=None,backend=None):
        with self._public_mutation():
            return self._save_owned(values,password,backend)

    def activate_postgresql(self,values,password=None,connector=None,store_factory=None):
        with self._public_mutation():
            profile,password=self.connection_values(values,self.password()[0] if password is None else password)
            self.test(profile,password,connector)
            if store_factory is None:
                from core import PostgresStore
                store_factory=PostgresStore
            store_factory(profile,password)
            return self._save_owned(profile,password,backend='postgresql')

    def activate_sqlite(self):
        with self._public_mutation():
            profile=self.profile()
            profile['backend']='sqlite'
            self._write_owned(profile)
            return profile

    def reset_postgresql(self,values,password,confirmation):
        with self._public_mutation():
            profile,password=self.connection_values(values,self.password()[0] if password is None else password)
            if type(confirmation) is not str or confirmation != profile['database']:
                raise ValueError('Bestätigung stimmt nicht mit dem Datenbanknamen überein. Es wurde nichts gelöscht.')
            connection=self._connect(profile,password)
            try:
                from core import _PostgresConnection
                from migrations import (CURRENT_SCHEMA_VERSION,
                                        _acquire_postgres_migration_lock,
                                        _postgres_expected_tables, migrate_postgres)
                with connection.transaction():
                    db=_PostgresConnection(connection)
                    _acquire_postgres_migration_lock(db)
                    db.execute('DROP SCHEMA IF EXISTS wissensraum CASCADE')
                    migrate_postgres(db,acquire_lock=False)
                    rows=db.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema='wissensraum' ORDER BY table_name").fetchall()
                    tables=[row['table_name'] if isinstance(row,dict) else row[0] for row in rows]
                    required=_postgres_expected_tables(CURRENT_SCHEMA_VERSION)|{'schema_migrations'}
                    version_row=db.execute(
                        'SELECT MAX(version) AS version FROM wissensraum.schema_migrations').fetchone()
                    version=version_row['version'] if isinstance(version_row,dict) else version_row[0]
                    if set(tables)!=required or version!=CURRENT_SCHEMA_VERSION:
                        raise ValueError('Anwendungsschema ist unvollständig.')
            except Exception:
                raise ValueError('Anwendungsschema konnte nicht zurückgesetzt werden. Datenbankrechte und Verbindung prüfen.') from None
            finally:
                connection.close()
            return {'database':profile['database'],'tables':tables}

    def open_store(self,sqlite_path):
        profile=self.profile()
        if profile['backend']=='postgresql':
            from core import PostgresStore
            connection,password=self.connection_values(profile)
            return PostgresStore(connection,password)
        from core import Store
        return Store(sqlite_path)

    def health(self,sqlite_path,force_sqlite=False):
        """Check the selected store without initializing or committing probe data."""
        profile={'backend':'sqlite'} if force_sqlite else self.profile()
        if profile['backend']=='sqlite':
            path=Path(sqlite_path)
            if not path.exists():
                if os.path.lexists(path):
                    raise ValueError('SQLite-Zielpfad ist nicht verwendbar.')
                _probe_missing_sqlite_target(path)
                return {'backend':'SQLite','missing':True}
            from core import Store
            return Store(path,initialize=False).health()
        connection,password=self.connection_values(profile)
        from core import PostgresStore
        return PostgresStore(connection,password,initialize=False).health()
