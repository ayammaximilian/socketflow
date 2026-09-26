import concurrent.futures
import heapq
import itertools
import queue
import socket as socket_module
import threading
import time
from typing import Any, Callable, Optional

from .exceptions import ExceptionType


_STOP = object()
_ANY_OWNER = object()


class MemoryBudget:
    """Shared memory accounting for all connection buffers."""

    def __init__(self, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._used_bytes = 0
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used_bytes

    def reserve(self, amount: int):
        if amount <= 0:
            return
        with self._lock:
            if self._used_bytes + amount > self._max_bytes:
                raise ExceptionType.Backpressure(
                    "Shared memory limit reached; reduce traffic or raise max_memory_bytes"
                )
            self._used_bytes += amount

    def release(self, amount: int):
        if amount <= 0:
            return
        with self._lock:
            self._used_bytes = max(0, self._used_bytes - amount)



class TimeoutScheduler:
    """Run request timeouts from a single shared thread.

    A dedicated thread per request is avoided; all deadlines are kept in one
    heap and fired by one daemon thread.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._deadlines = []
        self._cancelled = set()
        self._counter = itertools.count()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="socketflow-request-timeouts", daemon=True
        )
        self._thread.start()

    def schedule(self, callback: Callable[[], None], delay: Optional[float]):
        if delay is None:
            return None
        deadline = time.monotonic() + max(0.0, float(delay))
        entry = (deadline, next(self._counter), callback)
        with self._condition:
            if self._stopped:
                return None
            heapq.heappush(self._deadlines, entry)
            self._condition.notify()
        return entry

    def cancel(self, entry) -> bool:
        """Mark a timer cancelled in O(1).

        The entry stays in the heap and is discarded when it reaches the top,
        so cancelling many requests does not scan the whole list.
        """
        if entry is None:
            return False
        with self._condition:
            if entry[1] in self._cancelled:
                return False
            self._cancelled.add(entry[1])
            return True

    def _run(self):
        while True:
            with self._condition:
                if self._stopped:
                    return
                if not self._deadlines:
                    self._condition.wait(1.0)
                    continue

                # Drop cancelled entries sitting at the top of the heap.
                while self._deadlines and self._deadlines[0][1] in self._cancelled:
                    _, token, _ = heapq.heappop(self._deadlines)
                    self._cancelled.discard(token)

                if not self._deadlines:
                    continue

                deadline, _, _ = self._deadlines[0]
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                _, token, callback = heapq.heappop(self._deadlines)
                self._cancelled.discard(token)
            try:
                callback()
            except Exception:
                pass

    def shutdown(self):
        with self._condition:
            self._stopped = True
            self._deadlines.clear()
            self._cancelled.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)


class RequestHandle:
    """Future-like handle returned by non-blocking send calls.

    It supports the small Future surface used internally (``done``,
    ``set_result``, ``set_exception``) plus ``result``/``cancel``/
    ``add_done_callback`` for callers.
    """

    def __init__(
        self,
        registry: "PendingResponseRegistry",
        scheduler: TimeoutScheduler,
        data_id: str,
        owner: Any,
        timeout: Optional[float],
        timeout_message: str,
    ):
        self._registry = registry
        self._scheduler = scheduler
        self.data_id = data_id
        self.owner = owner
        self._future = concurrent.futures.Future()
        self._timeout_message = timeout_message
        self._entry = self._scheduler.schedule(self._on_timeout, timeout)

    def _on_timeout(self):
        if self._registry.remove(self.data_id, self, owner=self.owner):
            if not self._future.done():
                self._future.set_exception(
                    ExceptionType.NoResponse(self._timeout_message)
                )

    def done(self) -> bool:
        return self._future.done()

    def running(self) -> bool:
        return self._future.running()

    def cancelled(self) -> bool:
        return self._future.cancelled()

    def result(self, timeout: Optional[float] = None):
        return self._future.result(timeout)

    def exception(self, timeout: Optional[float] = None):
        return self._future.exception(timeout)

    def cancel(self) -> bool:
        if self._future.done():
            return False
        self._registry.remove(self.data_id, self, owner=self.owner)
        self._scheduler.cancel(self._entry)
        return self._future.cancel()

    def set_result(self, value):
        self._scheduler.cancel(self._entry)
        if not self._future.done():
            self._future.set_result(value)

    def set_exception(self, error):
        self._scheduler.cancel(self._entry)
        if not self._future.done():
            self._future.set_exception(error)

    def add_done_callback(self, callback):
        return self._future.add_done_callback(callback)


class PendingResponseRegistry:
    """Thread-safe storage for request/response handles."""

    def __init__(self):
        self._lock = threading.RLock()
        self._responses = {}

    @staticmethod
    def _key(data_id: str, owner: Any):
        return (owner, data_id)

    def add(self, data_id: str, future: Any, owner: Any = None):
        key = self._key(data_id, owner)
        with self._lock:
            if key in self._responses:
                raise ValueError(f"A request with data_id {data_id!r} is already pending")
            self._responses[key] = future

    def pop(self, data_id: str, owner: Any = None) -> Optional[Any]:
        with self._lock:
            return self._responses.pop(self._key(data_id, owner), None)

    def remove(self, data_id: str, future: Any, owner: Any = None) -> bool:
        key = self._key(data_id, owner)
        with self._lock:
            if self._responses.get(key) is not future:
                return False
            del self._responses[key]
            return True

    def drain(self, owner: Any = _ANY_OWNER):
        with self._lock:
            if owner is _ANY_OWNER:
                responses = list(self._responses.values())
                self._responses.clear()
                return responses

            keys = [key for key in self._responses if key[0] == owner]
            responses = [self._responses.pop(key) for key in keys]
            return responses

    def __eq__(self, other):
        if not isinstance(other, dict):
            return NotImplemented
        with self._lock:
            if not other:
                return not self._responses
            return False

    def __contains__(self, data_id: str) -> bool:
        with self._lock:
            return any(key[1] == data_id for key in self._responses)

    def __len__(self) -> int:
        with self._lock:
            return len(self._responses)


class SocketWriter:
    """Serialize socket writes and apply bounded outbound backpressure."""

    def __init__(
        self,
        sock: socket_module.socket,
        max_queue_bytes: int,
        max_pending_writes: int,
        name: str,
        memory_budget=None,
    ):
        if max_queue_bytes <= 0 or max_pending_writes <= 0:
            raise ValueError("Writer queue limits must be positive")

        self._socket = sock
        self._memory_budget = memory_budget
        self._max_queue_bytes = max_queue_bytes
        self._max_pending_writes = max_pending_writes
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._queued_bytes = 0
        self._pending_writes = 0
        self._closed = False
        self._error = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def pending_writes(self) -> int:
        with self._state_lock:
            return self._pending_writes

    @property
    def queued_bytes(self) -> int:
        with self._state_lock:
            return self._queued_bytes

    def send(self, data: bytes):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("SocketWriter.send expects bytes-like data")

        payload = bytes(data)
        with self._state_lock:
            if self._closed:
                if self._error is not None:
                    raise ExceptionType.MessageHandlerError(str(self._error))
                raise ExceptionType.NotConnected("Socket writer is closed")

            if (
                self._pending_writes >= self._max_pending_writes
                or self._queued_bytes + len(payload) > self._max_queue_bytes
            ):
                raise ExceptionType.Backpressure(
                    "Outbound socket queue is full; the peer is not keeping up"
                )

            if self._memory_budget is not None:
                self._memory_budget.reserve(len(payload))
            self._queued_bytes += len(payload)
            self._pending_writes += 1
            self._queue.put_nowait(payload)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued frames are written to the socket.

        Returns True if the queue drained, False if the timeout expired.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.pending_writes == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    def _complete(self, payload: bytes):
        with self._state_lock:
            self._queued_bytes -= len(payload)
            self._pending_writes -= 1
        if self._memory_budget is not None:
            self._memory_budget.release(len(payload))

    def _drain(self):
        while True:
            try:
                payload = self._queue.get_nowait()
            except queue.Empty:
                return

            try:
                if payload is not _STOP:
                    self._complete(payload)
            finally:
                self._queue.task_done()

    def _close_socket(self):
        try:
            self._socket.shutdown(socket_module.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            self._socket.close()
        except (OSError, AttributeError):
            pass

    def _fail(self, error: Exception):
        with self._state_lock:
            self._error = error
            self._closed = True
        self._close_socket()

    def _run(self):
        try:
            while True:
                payload = self._queue.get()
                if payload is _STOP:
                    self._queue.task_done()
                    return

                try:
                    self._socket.sendall(payload)
                except Exception as error:
                    self._complete(payload)
                    self._fail(error)
                    return
                else:
                    self._complete(payload)
                finally:
                    self._queue.task_done()
        except Exception as error:
            self._fail(error)
        finally:
            self._drain()

    def close(self, timeout: float = 5.0):
        with self._state_lock:
            already_closed = self._closed
            self._closed = True

        if not already_closed:
            self._queue.put_nowait(_STOP)

        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.abort(timeout=timeout)

    def abort(self, timeout: float = 1.0):
        with self._state_lock:
            self._closed = True
            if self._error is None:
                self._error = ExceptionType.NotConnected("Socket writer aborted")

        self._close_socket()
        self._drain()
        self._queue.put_nowait(_STOP)

        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
