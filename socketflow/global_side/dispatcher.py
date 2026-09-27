from typing import Dict, List, Callable, Any, Optional, Tuple, Deque
import concurrent.futures
import re
import threading
import time
from collections import deque

from .exceptions import ExceptionType
from .event import ErrorData, EventType


class EventDispatcher:
    def __init__(
        self,
        max_workers: int = 32,
        max_pending_tasks: int = 1000,
        queue_timeout: float = 1.0,
        owner: Any = None,
    ):
        if max_workers < 1 or max_pending_tasks < 1:
            raise ValueError("Dispatcher limits must be positive")
        if queue_timeout < 0:
            raise ValueError("queue_timeout cannot be negative")

        self._queue_timeout = queue_timeout
        self._task_slots = threading.BoundedSemaphore(max_pending_tasks)
        self._state_lock = threading.Lock()
        self._closed = False
        self._active_tasks = 0
        self._idle_condition = threading.Condition()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="socketflow-dispatch",
        )

        self._event_handlers: Dict[str, List[Callable]] = {}
        self._path_handlers: Dict[str, List[Callable]] = {}
        self._path_middleware: Dict[str, List[Callable]] = {}
        self._path_blocking: Dict[
            str, bool
        ] = {}
        # Blocking paths are serialised per (path, client) with a queue rather
        # than a lock. A lock makes every waiting message hold a worker thread,
        # so one busy client can exhaust the pool and stall everyone else. One
        # worker draining a queue keeps the backlog off the pool entirely.
        self._serial_lock = threading.Lock()
        self._serial_queues: Dict[Tuple[str, Any], Deque[Callable]] = {}
        self._serial_active: set = set()
        self._max_pending_tasks = max_pending_tasks
        self._path_patterns: List[Tuple[str, re.Pattern, List[str]]] = []
        self._path_pattern_handlers: Dict[str, List[Callable]] = {} 
        self._path_pattern_middleware: Dict[str, List[Callable]] = {} 
        self._path_pattern_blocking: Dict[str, bool] = {}  
        self._server = None
        self._client = None
        self.owner = owner

    def _run_task(self, callback):
        try:
            callback()
        finally:
            self._task_slots.release()
            with self._idle_condition:
                self._active_tasks -= 1
                if self._active_tasks == 0:
                    self._idle_condition.notify_all()

    def _submit(self, callback):
        if not self._task_slots.acquire(timeout=self._queue_timeout):
            raise ExceptionType.DispatcherError(
                "Dispatch queue is full; the application is not keeping up"
            )

        with self._state_lock:
            if self._closed:
                self._task_slots.release()
                raise ExceptionType.DispatcherError("Dispatcher is shut down")

        with self._idle_condition:
            self._active_tasks += 1

        try:
            return self._executor.submit(self._run_task, callback)
        except Exception:
            with self._idle_condition:
                self._active_tasks -= 1
                if self._active_tasks == 0:
                    self._idle_condition.notify_all()
            self._task_slots.release()
            raise

    @property
    def active_tasks(self) -> int:
        """Number of handler tasks that are queued or currently running."""
        with self._idle_condition:
            return self._active_tasks

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until no handler task is queued or running.

        Returns True if the dispatcher became idle, False if the timeout
        expired first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._idle_condition:
            while self._active_tasks > 0:
                if deadline is None:
                    self._idle_condition.wait(0.05)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._active_tasks == 0
                self._idle_condition.wait(min(remaining, 0.05))
            return True

    def shutdown(self, wait: bool = True):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)

    def event(self, event_type: str):
        """Decorator for event handlers"""

        def decorator(func):
            self.register_event(event_type, func)
            return func

        return decorator

    def path(self, path: str, middleware=None, block: bool = False):
        """Decorator for path handlers"""

        def decorator(func):
            self.register_path(path, func, middleware, block)
            return func

        return decorator

    def register_event(self, event_type: str, handler: Callable):
        """Register an event handler"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def _path_to_regex(self, path: str):
        """Convert a path with <param> placeholders to a regex pattern.

        Example: /file-manager/<ID> -> (^/file-manager/(?P<ID>[^/]+)$), ['ID']
        Returns (compiled_regex, param_names) or (None, []) if no placeholders.
        """
        param_names = re.findall(r"<(\w+)>", path)
        if not param_names:
            return None, []

        # Escape everything except the <param> placeholders, then replace them
        parts = re.split(r"(<\w+>)", path)
        regex_parts = []
        for part in parts:
            m = re.match(r"<(\w+)>", part)
            if m:
                regex_parts.append(f"(?P<{m.group(1)}>[^/]+)")
            else:
                regex_parts.append(re.escape(part))

        return re.compile("^" + "".join(regex_parts) + "$"), param_names

    def register_path(
        self, path: str, handler: Callable, middleware=None, block: bool = False
    ):
        """Register a path handler"""
        regex, param_names = self._path_to_regex(path)
        if regex is not None:
            # This is a parameterized path pattern
            # Register the pattern if not already known
            if not any(p[0] == path for p in self._path_patterns):
                self._path_patterns.append((path, regex, param_names))
            # Store handler
            if path not in self._path_pattern_handlers:
                self._path_pattern_handlers[path] = []
            self._path_pattern_handlers[path].append(handler)
            # Store middleware
            if middleware:
                if path not in self._path_pattern_middleware:
                    self._path_pattern_middleware[path] = []
                if isinstance(middleware, list):
                    self._path_pattern_middleware[path].extend(middleware)
                else:
                    self._path_pattern_middleware[path].append(middleware)
            # Store blocking
            if block:
                self._path_pattern_blocking[path] = True
        else:
            # Exact path match
            if path not in self._path_handlers:
                self._path_handlers[path] = []
            self._path_handlers[path].append(handler)

            # Track if this path has blocking enabled
            if block:
                self._path_blocking[path] = True

            # Register middleware separately if provided
            if middleware:
                if isinstance(middleware, list):
                    for m in middleware:
                        self.register_path_middleware(path, m)
                else:
                    self.register_path_middleware(path, middleware)

    def register_path_middleware(self, path: str, middleware: Callable):
        """Register path middleware"""
        if path not in self._path_middleware:
            self._path_middleware[path] = []
        self._path_middleware[path].append(middleware)

    def emit(self, event_type: str, data: Any):
        """Emit event"""
        if event_type not in self._event_handlers:
            return

        def _run_handlers():
            if event_type in self._event_handlers:
                for handler in self._event_handlers[event_type]:
                    try:
                        handler(data)
                    except Exception:
                        pass

        self._submit(_run_handlers)

    def _send_auto_reply(self, data, payload, status_code):
        """Answer a waiting caller on their own data_id, if we can."""
        if not getattr(data, "direct_response", False):
            return
        data_id = getattr(data, "data_id", None)
        if not data_id or self.owner is None:
            return
        try:
            if hasattr(self.owner, "send_client"):
                self.owner.send_client(
                    data.client_addr, payload, data_id, status_code=status_code
                )
            elif hasattr(self.owner, "send"):
                self.owner.send(payload, data_id=data_id, status_code=status_code)
        except Exception:
            pass

    def _report_handler_error(self, path, data, error):
        """Log a failed path handler and answer the caller with a 500."""
        try:
            self.emit(
                EventType.Global.ERROR,
                ErrorData(error=error, context=f"path:{path}"),
            )
        except Exception:
            pass

        self._send_auto_reply(
            data,
            {
                "error": "Internal Server Error",
                "detail": f"{type(error).__name__}: {error}",
            },
            500,
        )

    def _report_no_route(self, path, data):
        """Answer the caller with a 404 when no handler matches the path."""
        try:
            self.emit(
                EventType.Global.ERROR,
                ErrorData(
                    error=ExceptionType.PathNotFound(
                        f"No handler registered for path {path!r}"
                    ),
                    context=f"path:{path}",
                ),
            )
        except Exception:
            pass

        self._send_auto_reply(
            data,
            {
                "error": "Not Found",
                "detail": f"No handler registered for path {path!r}",
            },
            404,
        )

    def _enqueue_serial(self, key, job: Callable) -> None:
        """Queue one job to run alone on ``key``, starting a worker if needed.

        Only the first job for a key starts a drainer. That drainer stays on
        the pool for the whole backlog, so waiting messages cost queue space
        instead of a blocked worker thread. Jobs run in arrival order.
        """
        with self._serial_lock:
            pending = self._serial_queues.get(key)
            if pending is None:
                pending = deque()
                self._serial_queues[key] = pending
            if len(pending) >= self._max_pending_tasks:
                raise ExceptionType.DispatcherError(
                    "Dispatch queue is full; the application is not keeping up"
                )
            pending.append(job)
            if key in self._serial_active:
                return
            self._serial_active.add(key)

        self._submit(self._make_drainer(key))

    def _make_drainer(self, key) -> Callable:
        def _drain():
            while True:
                with self._serial_lock:
                    pending = self._serial_queues.get(key)
                    if not pending:
                        self._serial_queues.pop(key, None)
                        self._serial_active.discard(key)
                        return
                    job = pending.popleft()
                # _dispatch handles its own errors, so a failing job must not
                # strand the rest of this key's backlog.
                try:
                    job()
                except Exception:
                    pass

        return _drain

    def emit_path(self, path: str, data: Any):
        """Emit path event"""
        # Extract client identifier for blocking
        client_id = None
        if hasattr(data, "client_addr") and data.client_addr:
            client_id = data.client_addr
        elif hasattr(data, "server_addr") and data.server_addr:
            client_id = data.server_addr

        # Try exact match first, then pattern matching
        matched_pattern = None
        params = {}

        if path in self._path_handlers:
            # Exact match found
            handlers = self._path_handlers[path]
            middleware_list = self._path_middleware.get(path, [])
            is_blocking = self._path_blocking.get(path, False)
        else:
            # Try pattern matching
            for pat_str, regex, param_names in self._path_patterns:
                m = regex.match(path)
                if m:
                    matched_pattern = pat_str
                    params = m.groupdict()
                    handlers = self._path_pattern_handlers.get(pat_str, [])
                    middleware_list = self._path_pattern_middleware.get(pat_str, [])
                    is_blocking = self._path_pattern_blocking.get(pat_str, False)
                    break
            else:
                # No matching path or pattern. Route lookup happens here, on
                # the reader thread, so queue the reply off-thread.
                self._submit(lambda: self._report_no_route(path, data))
                return

        def _dispatch():
            # Run middleware first
            current_data = data
            for middleware_func in middleware_list:
                try:
                    result = middleware_func(current_data)
                    if result is False:  # Middleware rejected the request
                        return
                    elif result is not None:  # Middleware modified the data
                        current_data = result
                except Exception as e:
                    # Middleware failed, don't proceed to handlers
                    self._report_handler_error(path, current_data, e)
                    return

            # Run path handlers
            for handler in handlers:
                try:
                    if params:
                        try:
                            handler(current_data, **params)
                        except TypeError:
                            # Handler doesn't accept kwargs, pass without params
                            handler(current_data)
                    else:
                        handler(current_data)
                except Exception as e:
                    self._report_handler_error(path, current_data, e)

        if not is_blocking or not client_id:
            self._submit(_dispatch)
            return

        # Enqueue here rather than inside a worker: this runs on the reader
        # thread in arrival order, so a key's queue really is FIFO. Enqueuing
        # from a worker would let tasks race and reorder themselves.
        # Use the pattern string for parameterized paths so all dynamic
        # paths sharing the same pattern (e.g. /slow/1, /slow/2 for
        # /slow/<id>) share one queue per client.
        lock_path = matched_pattern if matched_pattern else path
        self._enqueue_serial((lock_path, client_id), _dispatch)

    def register_blueprint(self, blueprint):
        """Register a blueprint"""
        blueprint.register_with_dispatcher(self)

    def set_event_loop(self, loop):
        """Compatibility method - not needed for sync version"""
        pass
