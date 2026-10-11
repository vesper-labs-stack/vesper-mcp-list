"""Named log sources, confined paths and bounded read-only log inspection."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import codecs
import gzip
import json
import os
import posixpath
import re
import stat
import time

from dotenv import dotenv_values
from logs_ssh import open_log_session

DEFAULT_ENV = Path(__file__).resolve().with_name('.env')
NAME = re.compile(r'[a-z][a-z0-9_]*\Z')


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class LogProfile:
    target: Endpoint
    jump: Endpoint | None
    directories: dict[str, str]
    known_hosts: str
    encoding: str
    timeout: int


@dataclass(frozen=True)
class LogConfig:
    profiles: dict[str, LogProfile]
    scan_bytes: int
    output_bytes: int
    scan_seconds: int


def _integer(values, key, default, low, high):
    try:
        value = int(values.get(key, default))
    except (ValueError, TypeError):
        raise ValueError(f'{key} 必须为整数。') from None
    if not low <= value <= high:
        raise ValueError(f'{key} 必须在 {low} 到 {high} 之间。')
    return value


def _endpoint(values, prefix):
    for key in ('HOST', 'USER', 'PASSWORD'):
        if not values.get(prefix + key):
            raise ValueError(f'缺少日志配置 {prefix + key}。')
    return Endpoint(values[prefix + 'HOST'], _integer(values, prefix + 'PORT', 22, 1, 65535),
                    values[prefix + 'USER'], values[prefix + 'PASSWORD'])


def load_log_config(path=DEFAULT_ENV, environ: Mapping[str, str] | None = None):
    values = dict(dotenv_values(path, interpolate=False))
    values.update(os.environ if environ is None else environ)
    names = [name.strip().lower() for name in (values.get('LOG_SERVERS') or '').split(',')]
    if any(not NAME.fullmatch(name) for name in names) or len(set(names)) != len(names):
        raise ValueError('请配置 LOG_SERVERS，例如 order,payment；名称不能重复。')
    jump = _endpoint(values, 'LOG_JUMP_') if values.get('LOG_JUMP_HOST') else None
    if jump is None and any(values.get('LOG_JUMP_' + key) is not None for key in ('USER', 'PASSWORD', 'PORT')):
        raise ValueError('配置跳板机参数时必须填写 LOG_JUMP_HOST。')
    timeout = _integer(values, 'LOG_SSH_TIMEOUT', 10, 1, 60)
    config = LogConfig({}, _integer(values, 'LOG_SCAN_BYTES', 4194304, 1024, 33554432),
                       _integer(values, 'LOG_OUTPUT_BYTES', 65536, 1024, 1048576),
                       _integer(values, 'LOG_SCAN_SECONDS', 20, 1, 120))
    for name in names:
        prefix = f'LOG_{name.upper()}_'
        try:
            directories = json.loads(values.get(prefix + 'DIRS') or '')
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f'{prefix}DIRS 必须是 JSON 对象，例如 {{"app":"/data/app/logs"}}。') from None
        if not isinstance(directories, dict) or not directories:
            raise ValueError(f'{prefix}DIRS 必须包含至少一个日志目录别名。')
        for alias, root in directories.items():
            if not NAME.fullmatch(alias) or not isinstance(root, str) or not root.startswith('/') or '\\' in root or '\x00' in root or posixpath.normpath(root) == '/':
                raise ValueError(f'{prefix}DIRS 包含无效目录；不能授权根目录 /。')
            directories[alias] = posixpath.normpath(root)
        encoding = values.get(prefix + 'ENCODING') or 'utf-8'
        try:
            canonical_encoding = codecs.lookup(encoding).name
        except LookupError:
            raise ValueError(f'{prefix}ENCODING 编码不存在。') from None
        if canonical_encoding not in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'iso8859-1'):
            raise ValueError(f'{prefix}ENCODING 只支持 utf-8、utf-8-sig、gbk、gb18030、latin-1。')
        config.profiles[name] = LogProfile(
            _endpoint(values, prefix), jump, directories,
            values.get('LOG_KNOWN_HOSTS') or str(Path.home() / '.ssh' / 'known_hosts'),
            encoding, timeout)
    return config


def resolve_log_file(sftp, root, filename):
    if not isinstance(filename, str) or not filename or len(filename) > 512 or filename.startswith('/') or '\\' in filename or '\x00' in filename or any(part in ('', '.', '..') for part in filename.split('/')):
        raise ValueError('file 必须是日志目录内的相对文件名，禁止绝对路径和 ..。')
    real_root = posixpath.normpath(sftp.normalize(root))
    real_file = posixpath.normpath(sftp.normalize(posixpath.join(real_root, filename)))
    if real_root == '/' or not real_file.startswith(real_root + '/'):
        raise ValueError('日志路径或符号链接超出允许目录。')
    if not stat.S_ISREG(sftp.stat(real_file).st_mode):
        raise ValueError('只允许读取普通日志文件。')
    return real_file


def _count(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} 必须在 {low} 到 {high} 之间。')


def _deadline(deadline):
    if time.monotonic() > deadline:
        raise ValueError('日志扫描超过时间限制，请选择较小文件或降低扫描字节数。')


def _read_window(sftp, path, config, from_end, encoding, deadline):
    with sftp.open(path, 'rb', bufsize=0) as remote:
        size = remote.stat().st_size
        if path.endswith('.gz'):
            if from_end:
                raise ValueError('gzip 日志只支持从头搜索，请设置 from_end=false。')
            # Cap compressed input as well as decompressed output, including gzip headers.
            class LimitedInput:
                def __init__(self): self.used = 0
                def read(self, count=-1):
                    _deadline(deadline)
                    remaining = config.scan_bytes - self.used
                    if remaining <= 0:
                        if size <= self.used:
                            return b''
                        raise ValueError('gzip 压缩输入超过扫描上限，请选择较小的轮转文件。')
                    data = remote.read(min(remaining, 65536 if count < 0 else count))
                    self.used += len(data)
                    return data
            with gzip.GzipFile(fileobj=LimitedInput(), mode='rb') as stream:
                chunks = []
                total = 0
                while total <= config.scan_bytes:
                    _deadline(deadline)
                    chunk = stream.read(min(65536, config.scan_bytes + 1 - total))
                    if not chunk: break
                    chunks.append(chunk)
                    total += len(chunk)
                data = b''.join(chunks)
            incomplete = len(data) > config.scan_bytes
            data = data[:config.scan_bytes]
            start = 0
        else:
            start = max(0, size - config.scan_bytes) if from_end else 0
            boundary = b'\n'
            if start:
                remote.seek(start - 1)
                boundary = remote.read(1)
            remote.seek(start)
            amount = min(config.scan_bytes, size - start)
            chunks = []
            while amount > 0:
                _deadline(deadline)
                chunk = remote.read(min(amount, 65536))
                if not chunk: break
                chunks.append(chunk)
                amount -= len(chunk)
            data = b''.join(chunks)
            incomplete = size > config.scan_bytes
            if start and boundary != b'\n':
                cut = data.find(b'\n')
                data = data[cut + 1:] if cut >= 0 else b''
            if not from_end and size > len(data) and not data.endswith(b'\n'):
                data = data.rsplit(b'\n', 1)[0] if b'\n' in data else b''
        if path.endswith('.gz') and incomplete and not data.endswith(b'\n'):
            data = data.rsplit(b'\n', 1)[0] if b'\n' in data else b''
        return data.decode(encoding, errors='replace').splitlines(), incomplete, start


def _bounded(result, key, cap, keep_end=False, deadline=None):
    # Serialize each candidate once, accounting for the default JSON separators.
    empty = dict(result)
    empty[key] = []
    used = len(json.dumps(empty, ensure_ascii=False).encode('utf-8'))
    if used > cap:
        raise ValueError('结果元信息超过 LOG_OUTPUT_BYTES，请提高限制或缩短文件名。')
    rows = result[key]
    selected = []
    for row in reversed(rows) if keep_end else rows:
        if deadline is not None: _deadline(deadline)
        amount = len(json.dumps(row, ensure_ascii=False).encode('utf-8')) + (2 if selected else 0)
        if used + amount > cap:
            result['truncated'] = True
            break
        selected.append(row)
        used += amount
    result[key] = list(reversed(selected)) if keep_end else selected
    return result


class LogService:
    def __init__(self, env_path=DEFAULT_ENV, *, environ=None, session=None):
        self.env_path, self.environ = env_path, environ
        self.session = session or open_log_session
        self._config = None

    @property
    def config(self):
        if self._config is None:
            self._config = load_log_config(self.env_path, self.environ)
        return self._config

    def sources(self):
        return {'servers': [{'server': name, 'sources': list(profile.directories)}
                            for name, profile in self.config.profiles.items()]}

    def _source(self, server, source):
        profile = self.config.profiles.get(server.lower())
        if profile is None or source not in profile.directories:
            raise ValueError('未知服务器或日志别名，请先调用 list_log_sources。')
        return profile, profile.directories[source]

    def list_files(self, server, source):
        profile, root = self._source(server, source)
        result = {'files': [], 'truncated': False}
        deadline = time.monotonic() + self.config.scan_seconds
        with self.session(profile) as sftp:
            real_root = posixpath.normpath(sftp.normalize(root))
            if real_root == '/': raise ValueError('禁止列出根目录。')
            for index, entry in enumerate(sftp.listdir_iter(real_root, read_aheads=1)):
                _deadline(deadline)
                if index >= 500:
                    result['truncated'] = True
                    break
                if stat.S_ISREG(entry.st_mode):
                    result['files'].append({'file': entry.filename, 'bytes': entry.st_size,
                                            'mtime_epoch': entry.st_mtime})
        result['files'].sort(key=lambda item: item['mtime_epoch'] or 0, reverse=True)
        return _bounded(result, 'files', self.config.output_bytes, deadline=deadline)

    def tail(self, server, source, filename, lines=100):
        _count(lines, 'lines', 1, 1000)
        profile, root = self._source(server, source)
        deadline = time.monotonic() + self.config.scan_seconds
        with self.session(profile) as sftp:
            path = resolve_log_file(sftp, root, filename)
            text, incomplete, start = _read_window(sftp, path, self.config, True, profile.encoding, deadline)
        rows = [{'line_in_window': index + 1, 'text': line}
                for index, line in enumerate(text) if index >= len(text) - lines]
        result = {'file': filename, 'lines': rows, 'scan_start_byte': start,
                  'scan_truncated': incomplete, 'truncated': len(text) > lines or incomplete}
        return _bounded(result, 'lines', self.config.output_bytes, keep_end=True, deadline=deadline)

    def search(self, server, source, filename, keyword, context_lines=3, max_matches=20, from_end=True):
        if not isinstance(keyword, str) or not keyword or len(keyword) > 512:
            raise ValueError('keyword 必须为 1 到 512 字符的普通文本，不执行正则或命令。')
        _count(context_lines, 'context_lines', 0, 20)
        _count(max_matches, 'max_matches', 1, 100)
        if not isinstance(from_end, bool): raise ValueError('from_end 必须是布尔值。')
        profile, root = self._source(server, source)
        deadline = time.monotonic() + self.config.scan_seconds
        with self.session(profile) as sftp:
            path = resolve_log_file(sftp, root, filename)
            text, incomplete, start = _read_window(sftp, path, self.config, from_end, profile.encoding, deadline)
        matches = []
        more = False
        for index, line in enumerate(text):
            _deadline(deadline)
            if keyword in line:
                if len(matches) == max_matches:
                    more = True
                    break
                matches.append(index)
        selected = set()
        for index in matches:
            selected.update(range(max(0, index - context_lines), min(len(text), index + context_lines + 1)))
        result = {'file': filename, 'match_count': len(matches),
                  'lines': [{'line_in_window': index + 1, 'text': text[index], 'matched': index in matches}
                            for index in sorted(selected)],
                  'scan_start_byte': start, 'scan_truncated': incomplete,
                  'truncated': incomplete or more}
        return _bounded(result, 'lines', self.config.output_bytes, deadline=deadline)
