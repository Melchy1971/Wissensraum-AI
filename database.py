"""Local database profile and secure PostgreSQL connection handling."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


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
            return value if isinstance(value,dict) else {}
        except (OSError,UnicodeError,json.JSONDecodeError):
            return {}

    def profile(self):
        saved=self._file_config()
        backend=str(saved.get('backend') or self.environ.get('DB_BACKEND') or 'sqlite').lower()
        if backend not in ('sqlite','postgresql'):
            backend='sqlite'
        return {
            'backend':backend,
            'host':str(saved.get('host') or self.environ.get('DB_HOST') or ''),
            'port':str(saved.get('port') or self.environ.get('DB_PORT') or '5432'),
            'database':str(saved.get('database') or self.environ.get('DB_DATABASE') or ''),
            'username':str(saved.get('username') or self.environ.get('DB_USERNAME') or ''),
            'sslmode':str(saved.get('sslmode') or self.environ.get('DB_SSLMODE') or 'require'),
        }

    def password(self):
        value=self.vault.get(self.secret_name)
        if value:
            return value,'Betriebssystem-Schlüsselspeicher'
        value=str(self.environ.get('DB_PASSWORD') or '')
        return (value,'Umgebungsvariable') if value else ('','Nicht hinterlegt')

    @staticmethod
    def validate(values,password):
        host=str(values.get('host','')).strip()
        database=str(values.get('database','')).strip()
        username=str(values.get('username','')).strip()
        sslmode=str(values.get('sslmode') or 'require').strip()
        try:
            port=int(values.get('port') or 5432)
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
        password=str(password or '')
        if not password or len(password)>4096 or any(ord(c)<32 for c in password):
            raise ValueError('PostgreSQL-Kennwort eingeben.')
        return {'host':host,'port':port,'database':database,'username':username,'sslmode':sslmode},password

    def connection_values(self,values=None,password=None):
        values=values or self.profile()
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

    def _write(self,profile):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(self.path)

    def save(self,values,password=None,backend=None):
        current_password=self.password()[0]
        profile,password=self.connection_values(values,password or current_password)
        if password != current_password:
            self.vault.set(self.secret_name,password)
        profile['backend']=backend or self.profile()['backend']
        self._write(profile)
        return profile

    def activate_postgresql(self,values,password=None,connector=None,store_factory=None):
        profile,password=self.connection_values(values,password or self.password()[0])
        self.test(profile,password,connector)
        if store_factory is None:
            from core import PostgresStore
            store_factory=PostgresStore
        store_factory(profile,password)
        return self.save(profile,password,backend='postgresql')

    def activate_sqlite(self):
        profile=self.profile()
        profile['backend']='sqlite'
        self._write(profile)
        return profile

    def reset_postgresql(self,values,password,confirmation):
        profile,password=self.connection_values(values,password or self.password()[0])
        if str(confirmation) != profile['database']:
            raise ValueError('Bestätigung stimmt nicht mit dem Datenbanknamen überein. Es wurde nichts gelöscht.')
        connection=self._connect(profile,password)
        try:
            from core import POSTGRES_SCHEMA
            with connection.transaction():
                connection.execute('DROP SCHEMA IF EXISTS wissensraum CASCADE')
                for statement in (part.strip() for part in POSTGRES_SCHEMA.split(';')):
                    if statement:
                        connection.execute(statement)
        except Exception:
            raise ValueError('Anwendungsschema konnte nicht zurückgesetzt werden. Datenbankrechte und Verbindung prüfen.') from None
        finally:
            connection.close()
        from core import PostgresStore
        store=PostgresStore(profile,password)
        with store.connect() as db:
            tables=[row['table_name'] for row in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='wissensraum' ORDER BY table_name").fetchall()]
        required={'documents','chunks','ai_runs','settings','email_accounts','email_imports'}
        if set(tables)!=required:
            raise ValueError('Anwendungsschema ist unvollständig. Datenbankadministrator prüfen lassen.')
        return {'database':profile['database'],'tables':tables}

    def open_store(self,sqlite_path):
        profile=self.profile()
        if profile['backend']=='postgresql':
            from core import PostgresStore
            connection,password=self.connection_values(profile)
            return PostgresStore(connection,password)
        from core import Store
        return Store(sqlite_path)
