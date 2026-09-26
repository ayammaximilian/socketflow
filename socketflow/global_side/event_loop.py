import selectors
import socket as socket_module
import ssl
import threading
import time

from .exceptions import ExceptionType


class SelectorEventLoop:
    """Shared non-blocking selector loop for many server connections."""

    def __init__(self, name="socketflow-event-loop"):
        self._selector = selectors.DefaultSelector()
        self._selector_lock = threading.RLock()
        self._wake_reader, self._wake_writer = socket_module.socketpair()
        self._wake_reader.setblocking(False)
        self._wake_writer.setblocking(False)
        self._connections = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread = None
        self._name = name
        self._selector.register(
            self._wake_reader,
            selectors.EVENT_READ,
            None,
        )

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout=5.0):
        with self._lock:
            if not self._running:
                return
            self._running = False
        self.wake()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def wake(self):
        try:
            self._wake_writer.send(b"x")
        except OSError:
            pass

    def _drain_wake(self):
        try:
            while self._wake_reader.recv(4096):
                pass
        except OSError:
            pass

    def add_connection(
        self,
        sock,
        protocol,
        writer,
        on_close=None,
        max_queue_bytes=16 * 1024 * 1024,
        max_pending_writes=1000,
        memory_budget=None,
    ):
        with self._lock:
            if not self._running:
                raise RuntimeError("Event loop is not running")
            sock.setblocking(False)
            connection = _SelectorConnection(
                sock=sock,
                protocol=protocol,
                writer=writer,
                on_close=on_close,
                max_queue_bytes=max_queue_bytes,
                max_pending_writes=max_pending_writes,
                memory_budget=memory_budget,
                loop=self,
            )
            self._connections[id(connection)] = connection
            with self._selector_lock:
                self._selector.register(sock, selectors.EVENT_READ, connection)
                connection.update_interest()
        protocol._event_connection = connection
        self.wake()
        return connection

    def remove_connection(self, connection):
        with self._lock:
            self._connections.pop(id(connection), None)
        with self._selector_lock:
            try:
                self._selector.unregister(connection.socket)
            except (KeyError, ValueError, OSError):
                pass
        connection.close()

    def _run(self):
        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                with self._selector_lock:
                    events = self._selector.select(timeout=0.5)
            except OSError:
                continue
            for key, mask in events:
                if key.fileobj is self._wake_reader:
                    self._drain_wake()
                    continue
                connection = key.data
                try:
                    if mask & selectors.EVENT_READ:
                        connection.read_ready()
                    if mask & selectors.EVENT_WRITE:
                        connection.write_ready()
                except Exception:
                    self.remove_connection(connection)
            with self._lock:
                connections = list(self._connections.values())
            for connection in connections:
                try:
                    connection.protocol.keepalive_tick()
                except Exception:
                    self.remove_connection(connection)

    def close(self):
        self.stop()
        with self._lock:
            connections = list(self._connections.values())
        for connection in connections:
            self.remove_connection(connection)
        with self._selector_lock:
            try:
                self._selector.unregister(self._wake_reader)
            except (KeyError, ValueError, OSError):
                pass
            self._selector.close()
        self._wake_reader.close()
        self._wake_writer.close()


class EventLoopWriter:
    def __init__(self, max_queue_bytes, max_pending_writes, memory_budget=None):
        self.max_queue_bytes = max_queue_bytes
        self.max_pending_writes = max_pending_writes
        self.memory_budget = memory_budget
        self.connection = None

    def bind(self, connection):
        self.connection = connection

    def send(self, data):
        if self.connection is None:
            raise ExceptionType.NotConnected("Connection is not registered")
        self.connection.send(data)

    @property
    def pending_writes(self) -> int:
        return self.connection.pending_writes if self.connection else 0

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued frames are written to the socket."""
        if self.connection is None:
            return True
        return self.connection.flush(timeout)

    def abort(self, timeout=1.0):
        if self.connection is not None:
            self.connection.close()


class _SelectorConnection:
    def __init__(
        self,
        sock,
        protocol,
        writer,
        on_close,
        max_queue_bytes,
        max_pending_writes,
        memory_budget,
        loop,
    ):
        self.socket = sock
        self.protocol = protocol
        self.on_close = on_close
        self.loop = loop
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._queued_bytes = 0
        self._pending_writes = 0
        self._closed = False
        self._max_queue_bytes = max_queue_bytes
        self._max_pending_writes = max_pending_writes
        self._memory_budget = memory_budget
        writer.bind(self)
        self.writer = writer

    def send(self, data):
        payload = bytes(data)
        with self._lock:
            if self._closed:
                raise ExceptionType.NotConnected("Connection is closed")
            if (
                self._pending_writes >= self._max_pending_writes
                or self._queued_bytes + len(payload) > self._max_queue_bytes
            ):
                raise ExceptionType.Backpressure(
                    "Outbound socket queue is full; the peer is not keeping up"
                )
            if self._memory_budget is not None:
                self._memory_budget.reserve(len(payload))
            self._buffer.extend(payload)
            self._queued_bytes += len(payload)
            self._pending_writes += 1
        self.update_interest()
        self.loop.wake()

    @property
    def pending_writes(self) -> int:
        with self._lock:
            return self._pending_writes

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until the send buffer is empty.

        The event loop drains the buffer, so this only observes progress.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.pending_writes == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    def _consume_sent(self, amount):
        with self._lock:
            del self._buffer[:amount]
            self._queued_bytes = max(0, self._queued_bytes - amount)
            if not self._buffer:
                self._pending_writes = 0
        if self._memory_budget is not None:
            self._memory_budget.release(amount)

    def write_ready(self):
        while True:
            with self._lock:
                if not self._buffer or self._closed:
                    return
                payload = bytes(self._buffer)
            try:
                sent = self.socket.send(payload)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                return
            except (BlockingIOError, InterruptedError):
                return
            if sent <= 0:
                return
            self._consume_sent(sent)
            if sent < len(payload):
                return
        self.update_interest()

    def read_ready(self):
        try:
            data = self.socket.recv(65536)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return
        except (BlockingIOError, InterruptedError):
            return
        if not data:
            self.loop.remove_connection(self)
            return
        self.protocol.handle_data(data)
        self.write_ready()

    def update_interest(self):
        with self._lock:
            events = selectors.EVENT_READ
            if self._buffer:
                events |= selectors.EVENT_WRITE
        try:
            with self.loop._selector_lock:
                self.loop._selector.modify(self.socket, events, self)
        except (KeyError, ValueError, OSError):
            try:
                with self.loop._selector_lock:
                    self.loop._selector.register(self.socket, events, self)
            except (KeyError, ValueError, OSError):
                return

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            amount = self._queued_bytes
            self._buffer.clear()
            self._queued_bytes = 0
            self._pending_writes = 0
        if self._memory_budget is not None and amount:
            self._memory_budget.release(amount)
        try:
            self.socket.close()
        except OSError:
            pass
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
