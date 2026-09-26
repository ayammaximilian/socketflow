"""Structured logging for SocketFlow.

A small logging layer that emits records with fields, so log output can be
machine-read instead of free-text scraping. Nothing is printed unless a sink is
configured, which keeps the library quiet inside applications that do their own
logging.

    from socketflow import logs

    logs.configure(level=logs.LogLevel.INFO, json_output=True)
"""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TextIO


class LogLevel:
    """Log level constants (higher means more important)."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
    OFF = 100

    _NAMES = {
        DEBUG: "DEBUG",
        INFO: "INFO",
        WARNING: "WARNING",
        ERROR: "ERROR",
        CRITICAL: "CRITICAL",
        OFF: "OFF",
    }

    @classmethod
    def name(cls, level: int) -> str:
        return cls._NAMES.get(level, str(level))

    @classmethod
    def parse(cls, value) -> int:
        """Turn 'info' or 'WARNING' into a level number."""
        if isinstance(value, int):
            return value
        text = str(value).strip().upper()
        for level, name in cls._NAMES.items():
            if name == text:
                return level
        raise ValueError(f"Unknown log level: {value!r}")


@dataclass
class LogRecord:
    """One log entry with structured fields."""

    timestamp: float
    level: int
    name: str
    message: str
    fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def level_name(self) -> str:
        return LogLevel.name(self.level)

    @property
    def time_iso(self) -> str:
        return (
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "time": self.time_iso,
            "level": self.level_name,
            "logger": self.name,
            "message": self.message,
            **self.fields,
        }


def format_text(record: LogRecord) -> str:
    """Human-readable single line."""
    stamp = datetime.fromtimestamp(record.timestamp).strftime("%H:%M:%S")
    head = f"{stamp} {record.level_name:<8} {record.name}: {record.message}"
    if record.fields:
        extra = " ".join(f"{key}={value!r}" for key, value in record.fields.items())
        return f"{head} | {extra}"
    return head


def format_json(record: LogRecord) -> str:
    """One JSON object per line."""
    return json.dumps(record.as_dict(), default=str, sort_keys=True)


class LogSink:
    """Destination for log records."""

    def emit(self, record: LogRecord):
        raise NotImplementedError

    def close(self):
        pass


class StreamSink(LogSink):
    """Writes formatted records to a stream (stderr by default)."""

    def __init__(self, stream: Optional[TextIO] = None, json_output: bool = False):
        self._stream = stream
        self._json = json_output

    @property
    def stream(self) -> TextIO:
        # Looked up late so pytest-style capture redirection still works.
        return self._stream if self._stream is not None else sys.stderr

    def emit(self, record: LogRecord):
        line = format_json(record) if self._json else format_text(record)
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (ValueError, OSError):
            # A closed or broken stream must never break the application.
            pass


class MemorySink(LogSink):
    """Keeps records in memory. Useful for tests."""

    def __init__(self, limit: int = 1000):
        self.records: List[LogRecord] = []
        self._limit = limit
        self._lock = threading.Lock()

    def emit(self, record: LogRecord):
        with self._lock:
            self.records.append(record)
            if len(self.records) > self._limit:
                del self.records[: len(self.records) - self._limit]

    def messages(self, level: Optional[int] = None) -> List[str]:
        with self._lock:
            if level is None:
                return [record.message for record in self.records]
            return [r.message for r in self.records if r.level >= level]


class CallbackSink(LogSink):
    """Calls a function for every record."""

    def __init__(self, callback):
        self._callback = callback

    def emit(self, record: LogRecord):
        try:
            self._callback(record)
        except Exception:
            pass


class Logger:
    """Emits records to the configured sinks."""

    def __init__(self, name: str, config: "_Config", bound=None):
        self.name = name
        self._config = config
        self._bound = dict(bound or {})

    def bind(self, **fields) -> "Logger":
        """Return a child logger that always includes these fields."""
        merged = dict(self._bound)
        merged.update(fields)
        return Logger(self.name, self._config, merged)

    def is_enabled(self, level: int) -> bool:
        return level >= self._config.level

    def _clip(self, text: str) -> str:
        """Shorten a message so one huge value cannot flood a sink."""
        limit = self._config.max_message_length
        if limit is None or len(text) <= limit:
            return text
        return text[:limit] + f"... [truncated {len(text) - limit} chars]"

    def _log(self, level: int, message, fields: Dict[str, Any]):
        if not self.is_enabled(level):
            return
        merged = dict(self._bound)
        for key, value in fields.items():
            if isinstance(value, str):
                merged[key] = self._clip(value)
            else:
                merged[key] = value
        self._config.emit(
            LogRecord(
                timestamp=time.time(),
                level=level,
                name=self.name,
                message=self._clip(str(message)),
                fields=merged,
            )
        )

    def debug(self, message, **fields):
        self._log(LogLevel.DEBUG, message, fields)

    def info(self, message, **fields):
        self._log(LogLevel.INFO, message, fields)

    def warning(self, message, **fields):
        self._log(LogLevel.WARNING, message, fields)

    def error(self, message, **fields):
        self._log(LogLevel.ERROR, message, fields)

    def critical(self, message, **fields):
        self._log(LogLevel.CRITICAL, message, fields)

    def exception(self, message, error: BaseException, **fields):
        """Log an error with its type and message as fields."""
        self._log(
            LogLevel.ERROR,
            message,
            {
                "error_type": type(error).__name__,
                "error": str(error),
                **fields,
            },
        )


class _Config:
    """Shared logging configuration."""

    def __init__(self):
        self._lock = threading.RLock()
        self.level = LogLevel.OFF
        self.max_message_length: Optional[int] = None
        self.sinks: List[LogSink] = []
        self.loggers: Dict[str, Logger] = {}

    def configure(
        self,
        level=LogLevel.INFO,
        sinks: Optional[List[LogSink]] = None,
        json_output: bool = False,
        stream: Optional[TextIO] = None,
        max_message_length: Optional[int] = None,
    ):
        """Set the level and sinks used by every logger.

        Passing no sinks installs a stderr sink (JSON when json_output is True).
        Pass sinks=[] to silence logging entirely.

        max_message_length caps how long a single message or string field may
        be. It is None (no limit) by default; set it to protect a sink from a
        single huge value.
        """
        with self._lock:
            self.level = LogLevel.parse(level) if level is not None else LogLevel.OFF
            if max_message_length is not None and max_message_length < 0:
                raise ValueError("max_message_length cannot be negative")
            self.max_message_length = max_message_length
            if sinks is None:
                sinks = [StreamSink(stream=stream, json_output=json_output)]
            self.sinks = list(sinks)

    def reset(self):
        """Turn logging off and drop all sinks."""
        with self._lock:
            self.level = LogLevel.OFF
            self.max_message_length = None
            self.sinks = []
            self.loggers.clear()

    def get_logger(self, name: str) -> Logger:
        with self._lock:
            logger = self.loggers.get(name)
            if logger is None:
                logger = Logger(name, self)
                self.loggers[name] = logger
            return logger

    def emit(self, record: LogRecord):
        with self._lock:
            sinks = list(self.sinks)
        for sink in sinks:
            try:
                sink.emit(record)
            except Exception:
                # Logging must never raise into application code.
                pass


_config = _Config()


def configure(
    level=LogLevel.INFO,
    sinks=None,
    json_output=False,
    stream=None,
    max_message_length=None,
):
    """Configure logging for the whole library."""
    _config.configure(
        level=level,
        sinks=sinks,
        json_output=json_output,
        stream=stream,
        max_message_length=max_message_length,
    )


def reset():
    """Disable logging and clear all sinks."""
    _config.reset()


def get_logger(name: str) -> Logger:
    """Get a named logger, e.g. get_logger("socketflow.server")."""
    return _config.get_logger(name)


def current_level() -> int:
    return _config.level


def is_enabled(level: int) -> bool:
    return level >= _config.level
