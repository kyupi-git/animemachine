"""Portable settings/account backups and restart-time transactional restoration."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .config import credentials
from .config.policy import ConfigStore


MAX_UPLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
TABLES = {'accounts': ('auth_user','hidden_work','usage_notice'),
          'viewing': ('viewing_anchor','viewing_episode'), 'follows': ('release_follow',)}


def _json(value: Any) -> bytes:
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')


def _atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary = tempfile.mkstemp(prefix='.backup-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        credentials._restrict_permissions(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _snapshot(path: Path, tables: tuple[str,...]) -> dict[str,Any]:
    result = {}
    if not path.is_file():
        return result
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
        db.execute('BEGIN')
        for table in tables:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                cursor = db.execute(f'SELECT * FROM {table}')
                result[table] = {'columns':[field[0] for field in cursor.description], 'rows':cursor.fetchall()}
    return result


def database_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True,exist_ok=True)
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)) as reader:
        with contextlib.closing(sqlite3.connect(target,timeout=15)) as writer:
            reader.backup(writer,pages=256,sleep=.01)
            if writer.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('backup_database_invalid')
    credentials._restrict_permissions(target)


def read_package(raw: bytes) -> dict[str,Any]:
    if not raw or len(raw)>MAX_UPLOAD:
        raise ValueError('backup_package_invalid')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries)!=2 or {item.filename for item in entries}!={'manifest.json','data.json'}
                    or any(item.flag_bits&1 or item.file_size>MAX_EXPANDED for item in entries)):
                raise ValueError('backup_package_invalid')
            with archive.open('data.json') as stream:
                body = stream.read(MAX_EXPANDED+1)
            if len(body)>MAX_EXPANDED:
                raise ValueError('backup_package_invalid')
            with archive.open('manifest.json') as stream:
                manifest = json.loads(stream.read(65537))
            if (not isinstance(manifest,dict) or manifest.get('format')!='AnimeMachine.backup'
                    or manifest.get('formatVersion')!=1 or manifest.get('sha256')!=hashlib.sha256(body).hexdigest()):
                raise ValueError('backup_checksum_failed')
            payload = json.loads(body)
            if not isinstance(payload,dict) or not isinstance(payload.get('stores'),dict):
                raise ValueError('backup_package_invalid')
            return payload
    except (zipfile.BadZipFile,KeyError,UnicodeError,json.JSONDecodeError,RecursionError,NotImplementedError) as exc:
        raise ValueError('backup_package_invalid') from exc


class Manager:
    def __init__(self, state: Path, config: ConfigStore, auth_path: Path, *, auth_enabled: bool) -> None:
        self.state, self.config, self.auth_enabled = state,config,auth_enabled
        self.root = state/'backup-restore'
        self.lock = threading.Lock()
        self.paths = {'accounts':auth_path,'viewing':auth_path.parent/'viewing.sqlite3',
                      'follows':auth_path.parent/'follows.sqlite3'}

    def export(self) -> bytes:
        stores = {name:_snapshot(self.paths[name],TABLES[name]) for name in ('viewing','follows','accounts')}
        secrets = {}
        for name in credentials.CREDENTIAL_FILES:
            path = credentials.credential_path(name,self.state)
            if path.is_file():
                secrets[name] = path.read_text(encoding='utf-8').strip()
        payload = {'version':__version__,'createdAt':int(time.time()),'authEnabled':self.auth_enabled,
                   'config':self.config.read_persistent(),'stores':stores,'credentials':secrets}
        body = _json(payload)
        if len(body)>MAX_EXPANDED:
            raise ValueError('backup_too_large')
        output = io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.writestr('data.json',body)
            archive.writestr('manifest.json',_json({'format':'AnimeMachine.backup','formatVersion':1,
                                                   'sha256':hashlib.sha256(body).hexdigest()}))
        raw = output.getvalue()
        if len(raw)>MAX_UPLOAD:
            raise ValueError('backup_too_large')
        return raw

    def _directory(self, token: str) -> Path:
        if not isinstance(token,str) or re.fullmatch('[0-9a-f]{32}',token) is None:
            raise ValueError('backup_preview_expired')
        directory = self.root/token
        if not directory.resolve().is_relative_to(self.root.resolve()) or directory.is_symlink():
            raise ValueError('backup_package_invalid')
        return directory

    def _discard(self, directory: Path) -> None:
        if self._directory(directory.name).resolve()!=directory.resolve():
            raise ValueError('backup_package_invalid')
        shutil.rmtree(directory)

    def _cleanup_previews(self) -> None:
        if not self.root.is_dir() or (self.root/'pending.json').exists():
            return
        candidates = []
        for path in self.root.iterdir():
            if not re.fullmatch('[0-9a-f]{32}',path.name) or not path.is_dir():
                continue
            try:
                directory=self._directory(path.name)
                created=json.loads((directory/'preview.json').read_text(encoding='utf-8'))['createdAt']
                candidates.append((float(created),directory))
            except (OSError,KeyError,ValueError,TypeError):
                continue
        for index,(created,path) in enumerate(sorted(candidates,reverse=True)):
            if index>=2 or not 0<=time.time()-created<=900:
                self._discard(path)

    def _validate(self, payload: dict[str,Any]) -> None:
        self.config.validate_for_write(payload.get('config'))
        secrets = payload.get('credentials',{})
        if (not isinstance(secrets,dict) or set(secrets)-set(credentials.CREDENTIAL_FILES)
                or any(not isinstance(value,str) or not 1<=len(value)<=16384 or '\x00' in value for value in secrets.values())):
            raise ValueError('backup_package_invalid')
        if set(payload['stores']) != set(TABLES):
            raise ValueError('backup_package_invalid')
        for name, store in payload['stores'].items():
            if not isinstance(store,dict) or set(store)-set(TABLES[name]):
                raise ValueError('backup_package_invalid')
            for table, data in store.items():
                if (not isinstance(data,dict) or not isinstance(data.get('columns'),list)
                        or not isinstance(data.get('rows'),list) or len(data['rows'])>500000
                        or len(data['columns'])!=len(set(data['columns']))):
                    raise ValueError('backup_package_invalid')
                for row in data['rows']:
                    if not isinstance(row,(list,tuple)) or len(row)!=len(data['columns']):
                        raise ValueError('backup_package_invalid')
                    for value in row:
                        if (value is not None and type(value) not in {str,int,float,bool}
                                or isinstance(value,float) and not math.isfinite(value)
                                or isinstance(value,str) and len(value)>2*1024*1024):
                            raise ValueError('backup_package_invalid')
                if table=='auth_user':
                    for row in data['rows']:
                        user = dict(zip(data['columns'],row))
                        if (type(user.get('id')) is not int or user['id']<1 or user.get('role') not in {'admin','user'}
                                or not isinstance(user.get('username'),str)
                                or re.fullmatch(r'scrypt\$32768\$8\$1\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}',str(user.get('password_hash') or '')) is None):
                            raise ValueError('backup_accounts_invalid')

    def _prepare_database(self, name: str, data: dict[str,Any], target: Path) -> None:
        database_copy(self.paths[name],target)
        with contextlib.closing(sqlite3.connect(target)) as db,db:
            db.execute('PRAGMA foreign_keys=OFF')
            for table in TABLES[name]:
                actual = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                if not actual:
                    raise ValueError('backup_schema_incompatible')
                db.execute(f'DELETE FROM {table}')
                if table not in data:
                    continue
                columns,rows = data[table]['columns'],data[table]['rows']
                if not columns or not set(columns)<=actual:
                    raise ValueError('backup_schema_incompatible')
                db.executemany(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',rows)
            if name=='accounts':
                db.execute('DELETE FROM auth_session')
                if self.auth_enabled and not db.execute("SELECT 1 FROM auth_user WHERE enabled=1 AND role='admin'").fetchone():
                    raise ValueError('backup_admin_required')
            elif name=='follows':
                db.execute('UPDATE release_follow SET lease=NULL,lease_until=0')
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('backup_database_invalid')

    def preview(self, stream: BinaryIO, length: int) -> dict[str,Any]:
        if not 0<length<=MAX_UPLOAD:
            raise ValueError('backup_package_invalid')
        raw = stream.read(length)
        if len(raw)!=length:
            raise ValueError('backup_package_invalid')
        payload = read_package(raw)
        self._validate(payload)
        with self.lock:
            self._cleanup_previews()
        token = uuid.uuid4().hex
        directory = self._directory(token)
        directory.mkdir(parents=True)
        credentials._restrict_permissions(directory,directory=True)
        try:
            for name in TABLES:
                self._prepare_database(name,payload['stores'][name],directory/f'{name}.sqlite3')
            _atomic(directory/'package.zip',raw)
            _atomic(directory/'preview.json',_json({'createdAt':time.time(),'sha256':hashlib.sha256(raw).hexdigest()}))
        except Exception as exc:
            try:
                self._discard(directory)
            except OSError as cleanup_error:
                exc.add_note(f'Backup staging cleanup failed: {type(cleanup_error).__name__}')
            raise
        return {'token':token,'version':str(payload.get('version') or ''),'createdAt':payload.get('createdAt'),
                'users':len(payload['stores']['accounts'].get('auth_user',{}).get('rows',[])),
                'viewingRecords':len(payload['stores']['viewing'].get('viewing_anchor',{}).get('rows',[])),
                'credentialsIncluded':bool(payload.get('credentials'))}

    def schedule(self, token: str) -> None:
        directory = self._directory(token)
        try:
            preview = json.loads((directory/'preview.json').read_text(encoding='utf-8'))
            if not 0<=time.time()-preview['createdAt']<=900:
                raise ValueError('backup_preview_expired')
            raw = (directory/'package.zip').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=preview['sha256']:
                raise ValueError('backup_checksum_failed')
            self._validate(read_package(raw))
        except (OSError,KeyError,json.JSONDecodeError) as exc:
            raise ValueError('backup_preview_expired') from exc
        with self.lock:
            if (self.root/'pending.json').exists():
                raise ValueError('backup_restore_pending')
            _atomic(self.root/'pending.json',_json({'token':token,'phase':'queued'}))

    def status(self) -> dict[str,Any]:
        if (self.root/'pending.json').is_file():
            return {'state':'restarting'}
        try:
            return json.loads((self.root/'result.json').read_text(encoding='utf-8'))
        except (FileNotFoundError,json.JSONDecodeError):
            return {'state':'idle'}

    def apply_pending(self) -> bool:
        marker = self.root/'pending.json'
        if not marker.exists():
            return False
        pending = json.loads(marker.read_text(encoding='utf-8'))
        directory = self._directory(pending['token'])
        try:
            raw = (directory/'package.zip').read_bytes()
            preview = json.loads((directory/'preview.json').read_text(encoding='utf-8'))
            if hashlib.sha256(raw).hexdigest()!=preview['sha256']:
                raise ValueError('backup_checksum_failed')
            payload = read_package(raw)
            self._validate(payload)
        except (OSError,ValueError,KeyError,TypeError):
            if pending.get('phase')=='applying':
                self._rollback(directory)
            _atomic(self.root/'result.json',_json({'state':'failed','at':int(time.time())}))
            marker.unlink()
            return False
        before = directory/'before'
        before.mkdir(exist_ok=True)
        # All preparation/validation precedes changes to the live stores. On an
        # interrupted restart, the staged stores can be applied again unchanged.
        if pending.get('phase')=='queued':
            for name,path in self.paths.items():
                database_copy(path,before/f'{name}.sqlite3')
            _atomic(before/'config.json',_json(self.config.read_persistent()))
            prior_credentials = {key:credentials.credential_path(key,self.state).read_text(encoding='utf-8')
                                 for key in credentials.CREDENTIAL_FILES if credentials.credential_path(key,self.state).is_file()}
            _atomic(before/'credentials.json',_json(prior_credentials))
            _atomic(marker,_json({**pending,'phase':'applying'}))
        try:
            for name,path in self.paths.items():
                database_copy(directory/f'{name}.sqlite3',path)
            self.config.write(payload['config'])
            credentials.store_many(payload.get('credentials',{}),self.state)
        except Exception:
            self._rollback(directory)
            _atomic(self.root/'result.json',_json({'state':'failed','at':int(time.time())}))
            marker.unlink()
            raise
        _atomic(self.root/'result.json',_json({'state':'complete','at':int(time.time())}))
        marker.unlink()
        self._discard(directory)
        return True

    def _rollback(self, directory: Path) -> None:
        before = directory/'before'
        for name,path in self.paths.items():
            database_copy(before/f'{name}.sqlite3',path)
        self.config.write(json.loads((before/'config.json').read_text(encoding='utf-8')))
        old = json.loads((before/'credentials.json').read_text(encoding='utf-8'))
        for name in credentials.CREDENTIAL_FILES:
            if name not in old:
                credentials.credential_path(name,self.state).unlink(missing_ok=True)
        credentials.store_many(old,self.state)


def pending(state: Path) -> bool:
    return (state/'backup-restore/pending.json').is_file()
