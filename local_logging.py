"""Local, structured JSON Lines operational logging.

The public API intentionally accepts only stable identifiers and a small set of
scalar metadata.  A final formatter sanitizes every string again before it is
written, so accidental use outside the public API does not bypass filtering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import stat
import threading
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Callable, Iterable, TextIO
from uuid import uuid4


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
REDACTED = "[REDACTED]"
OPERATIONAL_LOGGING_WARNING = "Warnung: Operatives Logging ist nicht verfügbar."

# Deliberately narrow. Additions should be reviewed as data-classification
# changes, not merely convenience changes.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "attempt",
        "backend",
        "count",
        "duration_ms",
        "item_count",
        "method",
        "operation",
        "outcome",
        "status",
        "success",
        "provider",
    }
)
STRING_METADATA_KEYS = frozenset(
    {"backend", "method", "operation", "outcome", "provider", "status"}
)
INTEGER_METADATA_KEYS = frozenset({"attempt", "count", "item_count"})
STANDARD_LEVELS = MappingProxyType(
    {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_HEADER_CREDENTIAL_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]*"
)
_HEADER_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*=\s*[^\r\n]*"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|"
    r"password|passwd|pwd|access[_-]?token|refresh[_-]?token|token|"
    r"api[_-]?key|client[_-]?secret|secret|connection[_-]?string)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URI_USERINFO_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@")
_URL_QUERY_RE = re.compile(r"(?i)([^\s?#]*)\?[^\s#]*")

FailureCallback = Callable[[dict[str, str]], None]

_DESTINATION_REGISTRY_LOCK = threading.Lock()
_ACTIVE_DESTINATIONS: set[tuple[object, ...]] = set()
_WINDOWS_LOCK_OFFSET = (1 << 31) - 1


def _canonical_destination(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    if os.name == "nt":
        # Win32 ignores trailing spaces and dots in ordinary path components.
        # Keep the drive/UNC anchor intact while making those aliases share one
        # ownership key and one actual write destination.
        parts = resolved.parts
        if parts:
            resolved = Path(parts[0], *(part.rstrip(" .") for part in parts[1:]))
    return os.path.normcase(str(resolved))


def _destination_key(canonical_path: str) -> tuple[object, ...]:
    try:
        status = os.stat(canonical_path)
    except OSError:
        return ("path", canonical_path)
    return ("file", status.st_dev, status.st_ino)


def _lock_directory(canonical_path: str) -> Path:
    """Return a process-environment-independent namespace for a path lock."""
    candidate = Path(canonical_path).parent
    while not candidate.is_dir() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate / ".local-json-logger-locks"


class _DestinationOwnership:
    """Hold process-local and OS-level ownership of one log destination."""

    def __init__(self, path: str | Path) -> None:
        self._canonical_path = _canonical_destination(path)
        self._lock_files: dict[tuple[object, ...], BinaryIO | TextIO] = {}
        self._borrowed_keys: set[tuple[object, ...]] = set()
        self._released = False
        self._state_lock = threading.RLock()
        path_key = ("path", self._canonical_path)
        self._claim_keys((path_key,))

    @property
    def destination(self) -> str:
        """The resolved destination represented by this ownership lock."""
        return self._canonical_path

    @staticmethod
    def _acquire_os_lock(lock_file: BinaryIO | TextIO) -> None:
        if os.name == "nt":
            import msvcrt

            position = lock_file.tell()
            try:
                # Keep the lock outside practical log content so readers are
                # not denied access to the first byte on Windows.
                lock_file.seek(_WINDOWS_LOCK_OFFSET)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            finally:
                lock_file.seek(position)
        else:
            import fcntl

            fcntl.flock(  # type: ignore[attr-defined]
                lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )

    @staticmethod
    def _release_os_lock(lock_file: BinaryIO | TextIO) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                position = lock_file.tell()
                try:
                    lock_file.seek(_WINDOWS_LOCK_OFFSET)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    lock_file.seek(position)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    def _claim_keys(
        self,
        keys: Iterable[tuple[object, ...]],
        *,
        identity_stream: TextIO | None = None,
    ) -> None:
        new_keys = tuple(key for key in keys if key not in self._lock_files)
        if not new_keys:
            return
        opened: dict[tuple[object, ...], BinaryIO | TextIO] = {}
        borrowed: set[tuple[object, ...]] = set()
        with _DESTINATION_REGISTRY_LOCK:
            if self._released or any(key in _ACTIVE_DESTINATIONS for key in new_keys):
                raise RuntimeError("log destination already has an active writer")
            _ACTIVE_DESTINATIONS.update(new_keys)
        try:
            lock_directory = _lock_directory(self._canonical_path)
            lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for key in new_keys:
                if identity_stream is not None and key[0] == "file":
                    lock_file = identity_stream
                    borrowed.add(key)
                else:
                    digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
                    lock_file = open(lock_directory / f"{digest}.lock", "a+b")
                opened[key] = lock_file
                self._acquire_os_lock(lock_file)
        except BaseException as exc:
            for key, lock_file in opened.items():
                self._release_os_lock(lock_file)
                if key not in borrowed:
                    try:
                        lock_file.close()
                    except BaseException:
                        pass
            with _DESTINATION_REGISTRY_LOCK:
                for key in new_keys:
                    _ACTIVE_DESTINATIONS.discard(key)
            if isinstance(exc, Exception):
                raise RuntimeError("log destination already has an active writer") from None
            raise
        self._lock_files.update(opened)
        self._borrowed_keys.update(borrowed)

    def bind_stream(self, stream: TextIO) -> None:
        """Bind the identity actually opened before any record is written."""
        status = os.fstat(stream.fileno())
        with self._state_lock:
            self._claim_keys(
                (("file", status.st_dev, status.st_ino),),
                identity_stream=stream,
            )

    def release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._released = True
            lock_files = self._lock_files
            self._lock_files = {}
            borrowed_keys = self._borrowed_keys
            self._borrowed_keys = set()
            for key, lock_file in lock_files.items():
                self._release_os_lock(lock_file)
                if key not in borrowed_keys:
                    try:
                        lock_file.close()
                    except OSError:
                        pass
            with _DESTINATION_REGISTRY_LOCK:
                for key in lock_files:
                    _ACTIVE_DESTINATIONS.discard(key)


def _prune_numbered_backups(destination: str, backup_count: int) -> None:
    """Remove only regular, unaliased numbered backups outside retention."""
    base = Path(destination)
    prefix = base.name + "."
    try:
        with os.scandir(base.parent) as scan:
            entries = tuple(scan)
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            suffix = entry.name[len(prefix):]
            if not suffix.isascii() or not suffix.isdigit():
                continue
            number = int(suffix)
            if 1 <= number <= backup_count:
                continue
            status = os.lstat(entry.path)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                continue
            os.unlink(entry.path)
    except (FileNotFoundError, NotADirectoryError):
        # Preserve delayed-open behavior for destinations whose parents do not
        # exist or are not directories; there can be no backups to prune.
        return
    except OSError:
        raise RuntimeError("log backup retention failed") from None


def _validate_identifier(value: str, field: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a stable identifier")
    return value


def _sanitize_text(value: str, known_secrets: tuple[str, ...]) -> str:
    sanitized = value
    # Shape-based redaction must happen before exact replacement. Otherwise a
    # known value such as "Bearer" or "token" can destroy the pattern while
    # leaving the adjacent credential exposed.
    sanitized = _URL_QUERY_RE.sub(r"\1?[REDACTED]", sanitized)
    sanitized = _URI_USERINFO_RE.sub(r"\1[REDACTED]@", sanitized)
    sanitized = _HEADER_CREDENTIAL_RE.sub(r"\1: [REDACTED]", sanitized)
    sanitized = _HEADER_CREDENTIAL_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", sanitized)
    sanitized = _BEARER_RE.sub("Bearer [REDACTED]", sanitized)
    sanitized = _CREDENTIAL_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", sanitized)
    # Longer values first prevents partial replacement from obscuring another
    # known value. Short values are intentionally not excluded.
    for secret in known_secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTED)
    return sanitized


def _safe_timestamp(created: float) -> str:
    value = datetime.fromtimestamp(created, timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_safe_scalar(key: str, value: object) -> bool:
    if key in STRING_METADATA_KEYS:
        return type(value) is str and _IDENTIFIER.fullmatch(value) is not None
    if key in INTEGER_METADATA_KEYS:
        return type(value) is int and value >= 0
    if key == "duration_ms":
        if type(value) is int:
            return value >= 0
        if type(value) is float:
            return math.isfinite(value) and value >= 0
        return False
    if key == "success":
        return type(value) is bool
    return False


def _safe_identifier(value: object, fallback: str) -> str:
    if type(value) is str and _IDENTIFIER.fullmatch(value):
        return value
    return fallback


def safe_exception_class(exception: BaseException) -> str:
    """Return an exact safe class identifier without metaclass dispatch."""
    try:
        name = type.__getattribute__(type(exception), "__name__")
    except BaseException:
        return "Exception"
    return _safe_identifier(name, "Exception")


def _safe_severity(record: logging.LogRecord) -> str:
    if type(record.levelno) is not int:
        return "UNKNOWN"
    level_name = record.levelname.upper() if type(record.levelname) is str else ""
    if STANDARD_LEVELS.get(level_name) == record.levelno:
        return level_name
    return "UNKNOWN"


class SafeJsonFormatter(logging.Formatter):
    """Render one sanitized UTF-8-safe JSON object per log record."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        normalized = {secret for secret in known_secrets if type(secret) is str and secret}
        self._known_secrets = tuple(sorted(normalized, key=len, reverse=True))

    def _clean(self, value: object) -> object:
        if type(value) is str:
            return _sanitize_text(value, self._known_secrets)
        if value is None or type(value) in {bool, int, float}:
            return value
        # This is the final boundary: never stringify arbitrary objects.
        return REDACTED

    def format(self, record: logging.LogRecord) -> str:
        raw_payload = getattr(record, "safe_payload", None)
        payload: dict[str, object] = {}
        if type(raw_payload) is dict:
            for key, value in dict.items(raw_payload):
                if type(key) is str:
                    payload[key] = value

        output: dict[str, object] = {
            "timestamp": _safe_timestamp(record.created),
            "severity": _safe_severity(record),
            "event": self._clean(_safe_identifier(payload.get("event"), "logging_record")),
            "component": self._clean(_safe_identifier(payload.get("component"), "logging")),
            "run_id": self._clean(_safe_identifier(payload.get("run_id"), "unknown")),
        }
        for key, value in payload.items():
            if key in {"event", "component", "run_id"}:
                continue
            if key in ALLOWED_METADATA_KEYS:
                if _is_safe_scalar(key, value):
                    output[key] = self._clean(value)
            elif key == "exception_class":
                output[key] = self._clean(_safe_identifier(value, "Exception"))
            elif key == "error_code" and type(value) is str and _ERROR_CODE.fullmatch(value):
                output[key] = self._clean(value)
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler whose failures expose no record or destination data."""

    def __init__(
        self,
        filename: str,
        *,
        ownership: _DestinationOwnership,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        self._ownership = ownership
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def _open(self):
        stream = super()._open()
        try:
            self._ownership.bind_stream(stream)
        except BaseException:
            stream.close()
            raise
        return stream

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if self.stream is None:
            self.stream = self._open()
        opened = os.fstat(self.stream.fileno())
        current = os.stat(self.baseFilename)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("log destination identity changed")
        return bool(super().shouldRollover(record))

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        setattr(record, "_logging_write_failed", True)


class LocalJsonLogger:
    """Thread-safe allow-listed API for local operational events."""

    def __init__(
        self,
        path: str | Path,
        component: str,
        *,
        run_id: str | None = None,
        known_secrets: Iterable[str] = (),
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        failure_callback: FailureCallback | None = None,
    ) -> None:
        self.component = _validate_identifier(component, "component")
        generated_run_id = uuid4().hex if run_id is None else run_id
        self.run_id = _validate_identifier(generated_run_id, "run_id")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if type(backup_count) is not int or not 1 <= backup_count <= 5:
            raise ValueError("backup_count must be between one and five")

        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._closed = False
        self._state_lock = threading.RLock()
        self._failure_callback = failure_callback
        self._callback_state = threading.local()
        self._ownership = _DestinationOwnership(path)
        try:
            _prune_numbered_backups(self._ownership.destination, backup_count)
            self._logger = logging.Logger(f"local-json-{uuid4().hex}", level=logging.DEBUG)
            self._logger.propagate = False
            self._handler = _SafeRotatingFileHandler(
                self._ownership.destination,
                ownership=self._ownership,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
            self._handler.setFormatter(SafeJsonFormatter(known_secrets))
            self._logger.addHandler(self._handler)
            # Existing files are opened immediately so their filesystem identity
            # is locked before a hard-link alias can acquire another writer.
            # Missing destinations retain the handler's delayed-open behavior.
            if os.path.isfile(self._ownership.destination):
                self._handler.stream = self._handler._open()
        except BaseException:
            handler = getattr(self, "_handler", None)
            if handler is not None:
                try:
                    handler.close()
                except BaseException:
                    pass
            self._ownership.release()
            raise

    def event(
        self,
        event_name: str,
        *,
        severity: str = "INFO",
        metadata: dict[str, object] | None = None,
        exception: BaseException | None = None,
        exception_class: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        """Write an event, returning ``False`` on a destination failure."""
        event_name = _validate_identifier(event_name, "event_name")
        if type(severity) is not str:
            raise ValueError("severity must be a standard logging severity")
        normalized_severity = severity.upper()
        level = STANDARD_LEVELS.get(normalized_severity)
        if level is None:
            raise ValueError("severity must be a standard logging severity")

        payload: dict[str, object] = {
            "event": event_name,
            "component": self.component,
            "run_id": self.run_id,
        }
        if metadata is not None:
            if type(metadata) is not dict:
                raise TypeError("metadata must be an exact dict")
            for key, value in metadata.items():
                if (
                    type(key) is str
                    and key in ALLOWED_METADATA_KEYS
                    and _is_safe_scalar(key, value)
                ):
                    payload[key] = value

        if exception is not None:
            payload["exception_class"] = safe_exception_class(exception)
            if error_code is None or type(error_code) is not str or not _ERROR_CODE.fullmatch(error_code):
                raise ValueError("exception events require a stable safe error_code")
            payload["error_code"] = error_code
        elif exception_class is not None:
            payload["exception_class"] = _safe_identifier(exception_class, "Exception")
            if error_code is None or type(error_code) is not str or not _ERROR_CODE.fullmatch(error_code):
                raise ValueError("exception events require a stable safe error_code")
            payload["error_code"] = error_code
        elif error_code is not None:
            if type(error_code) is not str or not _ERROR_CODE.fullmatch(error_code):
                raise ValueError("error_code must be a stable safe code")
            payload["error_code"] = error_code

        record = self._logger.makeRecord(
            self._logger.name,
            level,
            "",
            0,
            event_name,
            (),
            None,
            extra={"safe_payload": payload},
        )
        setattr(record, "_logging_write_failed", False)
        with self._state_lock:
            if self._closed:
                return False
            self._logger.handle(record)
        failed = bool(getattr(record, "_logging_write_failed", False))
        if failed:
            self._notify_failure()
        return not failed

    def _notify_failure(self) -> None:
        callback = self._failure_callback
        if callback is None or getattr(self._callback_state, "active", False):
            return
        self._callback_state.active = True
        try:
            callback(
                {
                    "event": "logging_destination_unavailable",
                    "component": self.component,
                }
            )
        except BaseException:
            # Logging and warning callbacks must never mask application work.
            pass
        finally:
            self._callback_state.active = False

    def close(self) -> None:
        """Flush and close the destination; safe to call repeatedly."""
        failed = False
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._logger.removeHandler(self._handler)
            try:
                self._handler.close()
            except BaseException:
                failed = True
            finally:
                self._ownership.release()
        if failed:
            self._notify_failure()


def _default_operational_warning(message: str) -> None:
    print(message, file=sys.stderr)


def warn_operational_logging_failure(
    warning_callback: Callable[[str], None] | None = None,
) -> None:
    """Emit the generic logging warning without exposing or raising details."""
    callback = warning_callback or _default_operational_warning
    try:
        callback(OPERATIONAL_LOGGING_WARNING)
    except BaseException:
        pass


def known_runtime_secrets(store: object) -> tuple[str, ...]:
    """Return secrets already held by an opened store without loading any new ones."""
    try:
        password = vars(store).get("password")
    except TypeError:
        return ()
    if type(password) is str and password:
        return (password,)
    return ()


def create_operational_logger(
    base: str | Path,
    component: str,
    warning_callback: Callable[[str], None] | None = None,
    *,
    known_secrets: Iterable[str] = (),
) -> LocalJsonLogger | None:
    """Create the application logger without exposing setup failure details."""
    def sanitized_warning(_failure: object = None) -> None:
        warn_operational_logging_failure(warning_callback)

    try:
        directory = Path(base) / "data" / "logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return LocalJsonLogger(
            directory / "wissensraum.jsonl",
            component,
            known_secrets=known_secrets,
            failure_callback=sanitized_warning,
        )
    except Exception:
        warn_operational_logging_failure(warning_callback)
        return None
