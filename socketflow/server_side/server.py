import concurrent.futures
import hmac
import select
import socket as socket_module
import ssl
import threading
import time
import uuid
from typing import Any, Optional

from ..global_side.compression import MultiCompressor
from ..global_side.dispatcher import EventDispatcher
from ..global_side.event import (
    ClientConnectData,
    ClientDisconnectData,
    ErrorData,
    EventType,
    MessageReceivedData,
    ResponseData,
    ServerDrainData,
    ServerStartData,
    ServerStopData,
)
from ..global_side.exceptions import ExceptionType
from ..global_side.message_handler import message_handler
from ..global_side.event_loop import EventLoopWriter, SelectorEventLoop
from ..global_side.transport import (
    MemoryBudget,
    PendingResponseRegistry,
    RequestHandle,
    SocketWriter,
    TimeoutScheduler,
)
from ..global_side.message_manager import message_manager
from ..global_side import logs
from ..global_side import metrics as metrics_module
from ..global_side import protocol as protocol_module
from ..global_side.protocol import PROTOCOL_VERSION


class TcpServerProtocol:
    def __init__(
        self,
        server,
        max_frame_size=8 * 1024 * 1024,
        max_queue_bytes=16 * 1024 * 1024,
        max_pending_writes=1000,
        allow_pickle=False,
        memory_budget=None,
        max_decompressed_size=16 * 1024 * 1024,
    ):
        self.server = server
        self.socket = None
        self.client_addr = None
        self.max_frame_size = max_frame_size
        self.max_decompressed_size = max_decompressed_size
        self.allow_pickle = allow_pickle
        self._memory_budget = memory_budget
        self._inbound_reserved = 0
        self._inbound_lock = threading.Lock()
        self._buffer = bytearray()
        self._last_ping_time = time.monotonic()
        self._last_received_at = time.monotonic()
        self._last_ping_at = time.monotonic()
        self._missed_pings = 0
        self._connection_lost = threading.Event()
        self._control_lock = threading.Lock()
        self._event_loop_mode = False
        self._event_connection = None
        self.protocol_version = None
        self.client_identity = None
        self.client_cert_valid = None
        self._authenticated = not server.auth_required
        self._handshake_completed = not server.require_handshake
        self._auth_deadline = None
        self._connect_notified = False
        self._writer = None
        self._max_queue_bytes = max_queue_bytes
        self._max_pending_writes = max_pending_writes

    def _read_client_identity(self):
        """Read the peer certificate identity for mutual TLS.

        Sets client_identity (the certificate common name) and
        client_cert_valid (False when a certificate was presented but could
        not be verified). Leaves both as None when no certificate is used.
        """
        sock = self.socket
        if sock is None or not self.server.tls_client_ca:
            return
        try:
            peer = sock.getpeercert()
        except (AttributeError, ValueError, OSError):
            return
        if not peer:
            return

        self.client_cert_valid = bool(peer.get("subject"))
        for group in peer.get("subject", ()):
            for key, value in group:
                if key == "commonName":
                    self.client_identity = value
                    return

    def attach_socket(self, socket, writer=None):
        self.socket = socket
        self._read_client_identity()
        if writer is None:
            writer = SocketWriter(
                socket,
                max_queue_bytes=self._max_queue_bytes,
                max_pending_writes=self._max_pending_writes,
                name="socketflow-server-writer",
                memory_budget=self._memory_budget,
            )
        else:
            self._event_loop_mode = True
        self._writer = writer

    def _send_server_ready(self):
        headers = {"type": "__server_ready__"}
        length_bytes, encoded_message = message_manager.encode_with_length(
            headers,
            {
                "ok": True,
                "protocol": self.server.protocol_versions,
                "protocol_version": self.server.protocol_version,
            },
        )
        self.send_control(length_bytes + encoded_message)

    def _send_auth_result(self, ok, error=None):
        body = {"ok": ok}
        if error:
            body["error"] = error
        headers = {"type": "__auth_ack__"}
        length_bytes, encoded_message = message_manager.encode_with_length(
            headers, body
        )
        self.send_control(length_bytes + encoded_message)

    def _send_handshake_ok(self):
        headers = {"type": "__handshake_ok__"}
        length_bytes, encoded_message = message_manager.encode_with_length(
            headers, {"ok": True}
        )
        self.send_control(length_bytes + encoded_message)
        self._handshake_completed = True

    def _notify_connected(self):
        if self._connect_notified:
            return
        self._connect_notified = True
        server = self.server
        server.metrics.increment("connections_accepted_total")
        server.metrics.gauge("connections_active", len(server._clients))
        server._log.info(
            "client connected",
            client_addr=self.client_addr,
            client_identity=self.client_identity,
            protocol_version=self.protocol_version,
            active_clients=len(server._clients),
        )
        server.dispatcher.emit(
            EventType.Server.CLIENT_CONNECT,
            ClientConnectData(
                client_addr=self.client_addr,
                transport=self.socket,
                client_identity=self.client_identity,
            ),
        )

    def _reserve_inbound(self, amount):
        if self._memory_budget is not None:
            self._memory_budget.reserve(amount)
        with self._inbound_lock:
            self._inbound_reserved += amount

    def _release_inbound(self, amount):
        with self._inbound_lock:
            released = min(amount, self._inbound_reserved)
            self._inbound_reserved -= released
        if self._memory_budget is not None and released:
            self._memory_budget.release(released)

    def _release_all_inbound(self):
        with self._inbound_lock:
            amount = self._inbound_reserved
            self._inbound_reserved = 0
        if self._memory_budget is not None and amount:
            self._memory_budget.release(amount)

    def _check_protocol_version(self, body) -> bool:
        """Verify the client's protocol range overlaps ours.

        Returns False if the connection was refused, so the caller can stop
        processing further frames.
        """
        server = self.server
        if not server._protocol_enabled:
            # Negotiation disabled; accept whatever the peer speaks.
            self.protocol_version = PROTOCOL_VERSION
            return True

        local = server.protocol_versions
        remote = body.get("protocol") if isinstance(body, dict) else None
        agreed = protocol_module.negotiate(local, remote)
        if agreed is None:
            reason = protocol_module.describe(local, remote)
            self._send_protocol_error(reason)
            self._writer.close(timeout=1)
            self.handle_connection_lost()
            return False

        self.protocol_version = agreed
        return True

    def _send_protocol_error(self, reason: str):
        try:
            headers = {"type": "__protocol_error__"}
            length_bytes, encoded_message = message_manager.encode_with_length(
                headers,
                {
                    "ok": False,
                    "error": reason,
                    "supported": self.server.protocol_versions,
                },
            )
            self.send_control(length_bytes + encoded_message)
        except Exception:
            pass

    def handle_data(self, data):
        """Handle incoming data from client"""
        self._reserve_inbound(len(data))
        self._buffer.extend(data)
        self._last_received_at = time.monotonic()
        self._missed_pings = 0
        if self.server.auth_required and not self._authenticated:
            self._auth_deadline = self._last_received_at + max(
                float(self.server.handshake_timeout), 1.0
            )

        offset = 0
        while len(self._buffer) - offset >= 4:
            msg_len = int.from_bytes(self._buffer[offset : offset + 4], byteorder="big")
            if msg_len > self.max_frame_size:
                error = ExceptionType.InvalidData(
                    f"Frame exceeds the maximum size of {self.max_frame_size} bytes"
                )
                self.server.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=error, context="server.handle_data"),
                )
                self.server._record_error("server.handle_data", error)
                raise error

            if len(self._buffer) - offset < 4 + msg_len:
                break

            start = offset + 4
            end = start + msg_len
            message_data = self._buffer[start:end]

            offset = end

            headers, body = message_handler.unpack_data(
                bytes(message_data),
                allow_pickle=self.allow_pickle,
                max_decompressed_size=self.max_decompressed_size,
            )
            if not headers or not isinstance(headers, dict):
                error_msg = (
                    "Invalid message format"
                    if headers is None
                    else "Received message with invalid headers format"
                )
                self.server.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.InvalidData(error_msg),
                        context="server.handle_data",
                    ),
                )
                continue

            msg_type = headers.get("type")
            if not msg_type:
                self.server.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.InvalidData(
                            "Received message without type"
                        ),
                        context="server.handle_data",
                    ),
                )
                continue

            if msg_type == "__ping__":
                pong_message = message_handler.create_pong()
                try:
                    self.send(pong_message)
                except Exception:
                    pass
            elif msg_type == "__auth__":
                self._auth_deadline = None
                if not self._check_protocol_version(body):
                    break
                if not self.server.auth_required:
                    self._authenticated = True
                    self._auth_deadline = None
                    self._send_auth_result(True)
                    self._send_handshake_ok()
                    self._notify_connected()
                elif self._authenticated:
                    self._send_auth_result(False, "Already authenticated")
                    self._writer.close(timeout=1)
                    self.handle_connection_lost()
                    break
                elif self.server.authenticate(body):
                    self._authenticated = True
                    self._auth_deadline = None
                    self._send_auth_result(True)
                    self._send_handshake_ok()
                    self._notify_connected()
                else:
                    self._send_auth_result(False, "Authentication failed")
                    self._writer.close(timeout=1)
                    self.handle_connection_lost()
                    break
            elif msg_type == "__user__":
                if (
                    self.server.allow_legacy_clients
                    and not self.server.auth_required
                ):
                    if not self._handshake_completed:
                        self._handshake_completed = True
                        self._notify_connected()
                if not self._authenticated or not self._handshake_completed:
                    self._send_auth_result(False, "Authentication required")
                    self._writer.close(timeout=1)
                    self.handle_connection_lost()
                    break
                path = headers.get("path")
                data_id = headers.get("id")
                wait_response = headers.get("wait_response", False)
                status_code = headers.get("status_code", 200)
                self.server.metrics.increment(
                    "messages_received_total", path=path or "none"
                )
                self.server._log.debug(
                    "message received",
                    client_addr=self.client_addr,
                    path=path,
                    data_id=data_id,
                )
                event_data = MessageReceivedData(
                    data=body,
                    client_addr=self.client_addr,
                    data_id=data_id,
                    direct_response=wait_response,
                    client_identity=self.client_identity,
                    status_code=status_code,
                )

                # Resolve pending wait_response futures
                future = (
                    self.server.pending_responses.pop(data_id, owner=self.client_addr)
                    if data_id
                    else None
                )
                if future is not None:
                    if not future.done():
                        future.set_result(
                            ResponseData(
                                data=body, data_id=data_id, status_code=status_code
                            )
                        )
                    continue

                if path:
                    self.server.dispatcher.emit_path(path, event_data)
                else:
                    self.server.dispatcher.emit(EventType.Server.MESSAGE, event_data)

        if offset > 0:
            try:
                del self._buffer[:offset]
            finally:
                self._release_inbound(offset)

    def send_control(self, data):
        """Send a control frame before normal application traffic."""
        if not self.socket:
            raise ExceptionType.NotConnected("Not connected to client")
        if self._event_loop_mode:
            self._writer.send(data)
            return
        with self._control_lock:
            self.socket.sendall(data)

    def send(self, data):
        """Queue data for serialized delivery to the client."""
        if not self.socket:
            raise ExceptionType.NotConnected("Not connected to client")
        if self._writer is None:
            self.attach_socket(self.socket)
        self.server.metrics.increment("messages_sent_total")
        self.server.metrics.increment("bytes_sent_total", len(data))
        self._writer.send(data)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued outbound frames for this client are written."""
        writer = self._writer
        if writer is None:
            return True
        try:
            return writer.flush(timeout)
        except Exception:
            return False

    def close(self):
        if self._writer is not None:
            self._writer.abort()

    def handle_connection_lost(self):
        """Handle client disconnection exactly once."""
        if self._connection_lost.is_set():
            return
        self._connection_lost.set()
        self._release_all_inbound()

        with self.server._clients_lock:
            if self.server._clients.get(self.client_addr) is self:
                self.server._clients.pop(self.client_addr, None)
        self.server.metrics.gauge("connections_active", len(self.server._clients))
        self.server._log.info(
            "client disconnected",
            client_addr=self.client_addr,
            client_identity=self.client_identity,
            active_clients=len(self.server._clients),
        )

        for future in self.server.pending_responses.drain(self.client_addr):
            if not future.done():
                future.set_exception(
                    ExceptionType.NotConnected(
                        f"Client {self.client_addr} disconnected"
                    )
                )

        try:
            self.server.dispatcher.emit(
                EventType.Server.CLIENT_DISCONNECT,
                ClientDisconnectData(
                    client_addr=self.client_addr,
                    transport=self.socket,
                    client_identity=self.client_identity,
                ),
            )
        except Exception:
            pass
        finally:
            self.close()
            if self.socket:
                self.socket.close()
            self.socket = None

    def keepalive_tick(self):
        """Send a keepalive probe when the reader polling interval expires."""
        interval = max(float(self.server.keepalive_interval), 0.1)
        now = time.monotonic()
        if self.server.auth_required and not self._authenticated:
            if self._auth_deadline is None:
                self._auth_deadline = now + max(
                    float(self.server.handshake_timeout), 1.0
                )
            elif now >= self._auth_deadline:
                self._send_auth_result(False, "Authentication timed out")
                self._writer.close(timeout=1)
                self.handle_connection_lost()
            return
        if now - self._last_ping_at < interval:
            return

        self._last_ping_at = now
        try:
            self.send(message_handler.create_ping())
        except Exception:
            return

        if now - self._last_received_at >= interval * max(
            self.server.keepalive_max_missed, 1
        ):
            try:
                self.server.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.KeepaliveTimeout("Keepalive timeout"),
                        context="server.keepalive",
                    ),
                )
            except Exception:
                pass
            self.handle_connection_lost()

    def keepalive_check(self):
        """Compatibility entry point for applications that start keepalive manually."""
        while self.server.is_connected(self.client_addr):
            self.keepalive_tick()
            time.sleep(min(1.0, max(float(self.server.keepalive_interval), 0.1)))


class TcpServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        compression_type: str = "zlib",
        compression_level: int = 6,
        compress: bool = True,
        keepalive_interval: float = 30.0,
        keepalive_max_missed: int = 3,
        recv_buffer_size: int = 65536,
        send_buffer_size: int = 65536,
        max_frame_size: int = 8 * 1024 * 1024,
        max_outbound_queue_bytes: int = 16 * 1024 * 1024,
        max_pending_writes: int = 1000,
        max_dispatch_workers: int = 32,
        max_pending_tasks: int = 1000,
        dispatch_queue_timeout: float = 1.0,
        max_connections: int = 1000,
        allow_pickle: bool = False,
        tls_enabled: bool = False,
        tls_certfile: Optional[str] = None,
        tls_keyfile: Optional[str] = None,
        tls_client_ca: Optional[str] = None,
        tls_require_client_cert: bool = False,
        tls_handshake_timeout: float = 10.0,
        auth_enabled: bool = False,
        auth_token: Optional[str] = None,
        auth_username: Optional[str] = None,
        auth_password: Optional[str] = None,
        auth_timeout: float = 30.0,
        require_handshake: bool = True,
        handshake_timeout: Optional[float] = None,
        allow_legacy_clients: bool = False,
        protocol_version: Optional[int] = None,
        min_protocol_version: Optional[int] = None,
        max_protocol_version: Optional[int] = None,
        max_memory_bytes: int = 256 * 1024 * 1024,
        max_decompressed_size: int = 16 * 1024 * 1024,
        use_event_loop: bool = True,
    ):
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        if max_frame_size < 1 or max_outbound_queue_bytes < 1 or max_pending_writes < 1:
            raise ValueError("Server transport limits must be positive")
        if max_memory_bytes < 1:
            raise ValueError("max_memory_bytes must be positive")
        if max_decompressed_size < 1:
            raise ValueError("max_decompressed_size must be positive")
        if tls_enabled and (not tls_certfile or not tls_keyfile):
            raise ValueError(
                "tls_certfile and tls_keyfile are required when TLS is enabled"
            )
        if tls_require_client_cert and not tls_enabled:
            raise ValueError(
                "tls_require_client_cert requires tls_enabled=True"
            )
        if tls_require_client_cert and not tls_client_ca:
            raise ValueError(
                "tls_client_ca is required when tls_require_client_cert is True"
            )
        if tls_handshake_timeout <= 0 or auth_timeout <= 0:
            raise ValueError("TLS and authentication timeouts must be positive")
        if handshake_timeout is not None and handshake_timeout <= 0:
            raise ValueError("handshake_timeout must be positive")
        if (auth_username is None) != (auth_password is None):
            raise ValueError(
                "auth_username and auth_password must be provided together"
            )
        if auth_token is not None and auth_username is not None:
            raise ValueError(
                "Use either auth_token or username/password authentication"
            )
        if auth_enabled and auth_token is None and auth_username is None:
            raise ValueError(
                "Authentication credentials are required when auth_enabled is True"
            )
        if min_protocol_version is not None and max_protocol_version is not None:
            if min_protocol_version > max_protocol_version:
                raise ValueError(
                    "min_protocol_version cannot exceed max_protocol_version"
                )

        self.host = host
        self.port = port
        self.metrics = metrics_module.MetricsRegistry()
        self._log = logs.get_logger("socketflow.server")
        self.metrics.describe("connections_accepted_total", "Client connections accepted")
        self.metrics.describe("connections_rejected_total", "Client connections rejected")
        self.metrics.describe("connections_active", "Currently connected clients")
        self.metrics.describe("messages_received_total", "Messages received from clients")
        self.metrics.describe("messages_sent_total", "Messages sent to clients")
        self.metrics.describe("errors_total", "Errors by context")
        self.metrics.describe("backpressure_total", "Rejections caused by full queues")
        self.metrics.describe("request_duration_seconds", "Request handler duration")
        self.metrics.describe("bytes_sent_total", "Bytes written to clients")
        self.metrics.gauge("connections_active", 0)
        self.dispatcher = EventDispatcher(
            max_workers=max_dispatch_workers,
            max_pending_tasks=max_pending_tasks,
            queue_timeout=dispatch_queue_timeout,
            owner=self,
        )
        self._server = None
        self._accept_thread = None
        self._draining = False
        self._drain_event = threading.Event()
        self._clients = {}
        self._clients_lock = threading.RLock()
        self._connection_threads = set()
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        self.compression_type = compression_type
        self.compression_level = compression_level
        self.compress = compress
        self.keepalive_interval = keepalive_interval
        self.keepalive_max_missed = keepalive_max_missed
        self.pending_responses = PendingResponseRegistry()
        self._request_timeouts = TimeoutScheduler()
        self.memory_budget = MemoryBudget(max_memory_bytes)
        self.use_event_loop = use_event_loop
        self._event_loop = None
        self.recv_buffer_size = recv_buffer_size
        self.send_buffer_size = send_buffer_size
        self.max_frame_size = max_frame_size
        self.max_outbound_queue_bytes = max_outbound_queue_bytes
        self.max_pending_writes = max_pending_writes
        self.max_decompressed_size = max_decompressed_size
        self.allow_pickle = allow_pickle
        self.tls_enabled = tls_enabled
        self.tls_certfile = tls_certfile
        self.tls_keyfile = tls_keyfile
        self.tls_client_ca = tls_client_ca
        self.tls_require_client_cert = tls_require_client_cert
        self.tls_handshake_timeout = tls_handshake_timeout
        self.auth_enabled = (
            auth_enabled or auth_token is not None or auth_username is not None
        )
        self.auth_required = auth_token is not None or auth_username is not None
        self.auth_token = auth_token
        self.auth_username = auth_username
        self.auth_password = auth_password
        self.auth_timeout = auth_timeout
        self.handshake_timeout = (
            auth_timeout if handshake_timeout is None else handshake_timeout
        )
        self.require_handshake = require_handshake
        self.allow_legacy_clients = allow_legacy_clients
        # 0.1.4 clients pickle their payloads; the safe JSON format is newer.
        if allow_legacy_clients:
            self.allow_pickle = True
        self.protocol_version = protocol_version
        self._protocol_enabled = protocol_module.is_enabled(
            protocol_version, min_protocol_version, max_protocol_version
        )
        self.protocol_versions = protocol_module.make_range(
            min_protocol_version, max_protocol_version
        )

    def _record_error(self, context: str, error: Optional[BaseException] = None):
        """Count and log an error at the point it is detected."""
        self.metrics.increment("errors_total", context=context)
        if error is not None:
            self._log.error(
                "error",
                context=context,
                error_type=type(error).__name__,
                error=str(error),
            )
        else:
            self._log.error("error", context=context)

    def authenticate(self, credentials):
        if not isinstance(credentials, dict):
            return False
        if self.auth_token is not None:
            supplied = credentials.get("token")
            return isinstance(supplied, str) and hmac.compare_digest(
                supplied, self.auth_token
            )
        if self.auth_username is not None:
            username = credentials.get("username")
            password = credentials.get("password")
            return (
                isinstance(username, str)
                and isinstance(password, str)
                and hmac.compare_digest(username, self.auth_username)
                and hmac.compare_digest(password, self.auth_password)
            )
        return True

    def _create_tls_context(self):
        if not self.tls_enabled:
            return None
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            if hasattr(context, "minimum_version") and hasattr(ssl, "TLSVersion"):
                context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.tls_certfile, self.tls_keyfile)

            if self.tls_client_ca:
                # Mutual TLS: ask for a client certificate and verify it
                # against the client CA.
                context.load_verify_locations(cafile=self.tls_client_ca)
                context.verify_mode = (
                    ssl.CERT_REQUIRED
                    if self.tls_require_client_cert
                    else ssl.CERT_OPTIONAL
                )
            elif self.tls_require_client_cert:
                raise ValueError(
                    "tls_client_ca is required when tls_require_client_cert is True"
                )
            return context
        except (OSError, ssl.SSLError) as error:
            raise ExceptionType.TlsError(f"TLS setup failed: {error}")

    def _connection_entry(self, client_socket, address):
        current_thread = threading.current_thread()
        try:
            self._handle_connection(client_socket, address)
        finally:
            try:
                client_socket.close()
            except OSError:
                pass
            with self._clients_lock:
                self._connection_threads.discard(current_thread)
            self._connection_slots.release()

    def _handle_connection(self, client_socket, address):
        if self.tls_enabled:
            try:
                client_socket.settimeout(self.tls_handshake_timeout)
                client_socket = self._tls_context.wrap_socket(
                    client_socket,
                    server_side=True,
                )
                client_socket.settimeout(None)
            except (ssl.SSLError, OSError) as error:
                try:
                    self.dispatcher.emit(
                        EventType.Global.ERROR,
                        ErrorData(
                            error=ExceptionType.TlsError(
                                f"TLS handshake failed: {error}"
                            ),
                            context="server.accept",
                        ),
                    )
                except Exception:
                    pass
                client_socket.close()
                return

        protocol = TcpServerProtocol(
            self,
            max_frame_size=self.max_frame_size,
            max_queue_bytes=self.max_outbound_queue_bytes,
            max_pending_writes=self.max_pending_writes,
            allow_pickle=self.allow_pickle,
            memory_budget=self.memory_budget,
            max_decompressed_size=self.max_decompressed_size,
        )
        protocol.client_addr = address

        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_RCVBUF, self.recv_buffer_size
        )
        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, self.send_buffer_size
        )
        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_KEEPALIVE, 1
        )
        try:
            client_socket.setsockopt(
                socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1
            )
        except (AttributeError, OSError):
            pass

        protocol.attach_socket(client_socket)
        try:
            protocol._send_server_ready()
        except Exception:
            protocol.handle_connection_lost()
            return
        with self._clients_lock:
            self._clients[address] = protocol

        try:
            if not self.auth_required:
                protocol._notify_connected()
        except Exception:
            protocol.handle_connection_lost()
            raise

        while True:
            try:
                pending = getattr(client_socket, "pending", None)
                if pending is not None and pending():
                    data = client_socket.recv(65536)
                else:
                    readable, _, _ = select.select([client_socket], [], [], 1.0)
                    if not readable:
                        protocol.keepalive_tick()
                        continue
                    data = client_socket.recv(65536)
                if not data:
                    break
                protocol.handle_data(data)
            except socket_module.timeout:
                protocol.keepalive_tick()
            except Exception as error:
                try:
                    self.dispatcher.emit(
                        EventType.Global.ERROR,
                        ErrorData(error=error, context="server.receive"),
                    )
                    self._record_error("server.receive", error)
                except Exception:
                    pass
                break
        protocol.handle_connection_lost()

    def start(self):
        """Start the server"""
        if self._server is not None:
            raise ExceptionType.NotConnected("Server is already started")
        self._draining = False
        self._drain_event.clear()
        try:
            self._tls_context = self._create_tls_context()
            self._server = socket_module.socket(
                socket_module.AF_INET, socket_module.SOCK_STREAM
            )
            self._server.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1
            )
            self._server.bind((self.host, self.port))
            self._server.listen(100)
            if self.use_event_loop:
                self._event_loop = SelectorEventLoop()
                self._event_loop.start()

            self.dispatcher.emit(
                EventType.Server.START, ServerStartData(host=self.host, port=self.port)
            )
            self._log.info(
                "server started",
                host=self.host,
                port=self.port,
                tls=self.tls_enabled,
                auth=self.auth_required,
                require_client_cert=self.tls_require_client_cert,
                use_event_loop=self.use_event_loop,
            )

            self._accept_thread = threading.Thread(
                target=self._accept_connections,
                name="socketflow-server-acceptor",
                daemon=True,
            )
            self._accept_thread.start()

        except Exception as e:
            if self._server:
                self._server.close()
                self._server = None
            try:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=e, context="server.start"),
                )
            except Exception:
                pass
            raise

    def _accept_event_loop_connection(self, client_socket, address):
        protocol = None
        registered = False
        try:
            if self.tls_enabled:
                client_socket.settimeout(self.tls_handshake_timeout)
                client_socket = self._tls_context.wrap_socket(
                    client_socket,
                    server_side=True,
                )
                client_socket.settimeout(None)

            protocol = TcpServerProtocol(
                self,
                max_frame_size=self.max_frame_size,
                max_queue_bytes=self.max_outbound_queue_bytes,
                max_pending_writes=self.max_pending_writes,
                allow_pickle=self.allow_pickle,
                memory_budget=self.memory_budget,
                max_decompressed_size=self.max_decompressed_size,
            )
            protocol.client_addr = address
            writer = EventLoopWriter(
                self.max_outbound_queue_bytes,
                self.max_pending_writes,
                self.memory_budget,
            )
            protocol.attach_socket(client_socket, writer=writer)
            with self._clients_lock:
                self._clients[address] = protocol
            protocol._event_loop_mode = False
            protocol._send_server_ready()
            protocol._event_loop_mode = True

            def on_close():
                with self._clients_lock:
                    if self._clients.get(address) is protocol:
                        self._clients.pop(address, None)
                protocol.handle_connection_lost()
                self._connection_slots.release()

            self._event_loop.add_connection(
                client_socket,
                protocol,
                writer,
                on_close=on_close,
                max_queue_bytes=self.max_outbound_queue_bytes,
                max_pending_writes=self.max_pending_writes,
                memory_budget=self.memory_budget,
            )
            registered = True
            if not self.auth_required and not self.require_handshake:
                protocol._notify_connected()
        except Exception as error:
            if protocol is not None:
                protocol.handle_connection_lost()
            with self._clients_lock:
                self._clients.pop(address, None)
            try:
                client_socket.close()
            except OSError:
                pass
            if not registered:
                self._connection_slots.release()
            try:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=error, context="server.accept"),
                )
            except Exception:
                pass

    def _accept_connections(self):
        """Accept incoming connections."""
        server_socket = self._server
        while self._server is server_socket:
            try:
                client_socket, address = server_socket.accept()
                if self._draining:
                    # Draining: refuse new work, let existing clients finish.
                    # The connection slot is acquired below, so nothing to
                    # release here.
                    client_socket.close()
                    continue
                if not self._connection_slots.acquire(blocking=False):
                    client_socket.close()
                    self.metrics.increment(
                        "connections_rejected_total", reason="max_connections"
                    )
                    self.metrics.increment("backpressure_total", kind="connections")
                    self._log.warning(
                        "connection rejected: server connection limit reached",
                        reason="max_connections",
                    )
                    try:
                        self.dispatcher.emit(
                            EventType.Global.ERROR,
                            ErrorData(
                                error=ExceptionType.Backpressure(
                                    "Server connection limit reached"
                                ),
                                context="server.accept",
                            ),
                        )
                    except Exception:
                        pass
                    continue

                if self.use_event_loop:
                    self._accept_event_loop_connection(client_socket, address)
                    continue

                connection_thread = threading.Thread(
                    target=self._connection_entry,
                    args=(client_socket, address),
                    name="socketflow-server-reader",
                    daemon=True,
                )
                with self._clients_lock:
                    self._connection_threads.add(connection_thread)
                try:
                    connection_thread.start()
                except Exception:
                    with self._clients_lock:
                        self._connection_threads.discard(connection_thread)
                    self._connection_slots.release()
                    client_socket.close()
                    raise
            except Exception as e:
                if self._server is not server_socket:
                    break
                try:
                    self.dispatcher.emit(
                        EventType.Global.ERROR,
                        ErrorData(error=e, context="server.accept"),
                    )
                    self._record_error("server.accept", e)
                except Exception:
                    pass

    @property
    def draining(self) -> bool:
        """True while the server is finishing in-flight work."""
        return self._draining

    def _wait_for_drain(self, timeout: Optional[float]) -> bool:
        """Wait until the dispatcher is idle and outbound replies are flushed.

        Returns True if everything finished within the timeout.
        """
        if timeout is None:
            deadline = None
        else:
            deadline = time.monotonic() + max(0.0, timeout)

        def remaining():
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        # 1. Let running and queued handlers finish.
        if not self.dispatcher.wait_idle(remaining()):
            return False

        # 2. Push any replies those handlers queued to the clients.
        with self._clients_lock:
            protocols = list(self._clients.values())

        completed = True
        for protocol in protocols:
            budget = remaining()
            if budget is not None and budget <= 0:
                completed = False
                continue
            if not self._flush_protocol(protocol, budget):
                completed = False

        # 3. Make sure no handler queued a reply after we looked.
        if not self.dispatcher.wait_idle(remaining()):
            return False

        with self._clients_lock:
            protocols = list(self._clients.values())
        for protocol in protocols:
            budget = remaining()
            if budget is not None and budget <= 0:
                completed = False
                continue
            if not self._flush_protocol(protocol, budget):
                completed = False

        return completed

    @staticmethod
    def _flush_protocol(protocol, budget: Optional[float]) -> bool:
        """Flush one connection, tolerating protocols without a writer."""
        flush = getattr(protocol, "flush", None)
        if flush is None:
            return True
        try:
            return bool(flush(5.0 if budget is None else budget))
        except Exception:
            return False

    def drain(self, timeout: Optional[float] = 10.0, reason: str = "shutdown") -> bool:
        """Stop accepting new clients and wait for in-flight work to finish.

        Existing clients stay connected while their handlers and queued
        replies complete. New connections are refused immediately.

        Returns True if everything finished before the timeout.
        """
        if self._server is None and not self._clients:
            self._drain_event.set()
            return True

        already_draining = self._draining
        self._draining = True
        if not already_draining:
            self._drain_event.clear()
            try:
                self.dispatcher.emit(
                    EventType.Server.DRAINING,
                    ServerDrainData(
                        host=self.host,
                        port=self.port,
                        connected_clients=len(self._clients),
                        reason=reason,
                    ),
                )
            except Exception:
                pass

        drained = self._wait_for_drain(timeout)
        self._log.info(
            "drain finished",
            drained=drained,
            active_clients=len(self._clients),
            active_tasks=self.dispatcher.active_tasks,
        )
        self._drain_event.set()
        return drained

    def stop(self, drain: bool = False, drain_timeout: Optional[float] = 10.0):
        """Stop accepting connections and close all client transports.

        With drain=True, in-flight handlers and queued replies are given a
        chance to finish first.
        """
        if drain:
            self.drain(drain_timeout)

        if self._server:
            try:
                self._server.shutdown(socket_module.SHUT_RDWR)
            except OSError:
                pass
            self._server.close()
            self._server = None

        accept_thread = self._accept_thread
        self._accept_thread = None
        if accept_thread and accept_thread is not threading.current_thread():
            accept_thread.join(timeout=2)

        if self._event_loop is not None:
            self._event_loop.close()
            self._event_loop = None

        with self._clients_lock:
            clients = list(self._clients.items())
            self._clients.clear()

        for client_addr, protocol in clients:
            if protocol.socket:
                try:
                    protocol.socket.shutdown(socket_module.SHUT_RDWR)
                except OSError:
                    pass
            protocol.handle_connection_lost()

        with self._clients_lock:
            connection_threads = list(self._connection_threads)
        current_thread = threading.current_thread()
        for thread in connection_threads:
            if thread is not current_thread:
                thread.join(timeout=2)

        try:
            self.dispatcher.emit(
                EventType.Server.STOP, ServerStopData(host=self.host, port=self.port)
            )
        except Exception:
            pass

    def metrics_snapshot(self) -> dict:
        """All recorded metrics as a plain nested dict."""
        return self.metrics.snapshot()

    def metrics_text(self) -> str:
        """All recorded metrics in Prometheus text format."""
        return self.metrics.prometheus()

    def shutdown(self, drain: bool = True, drain_timeout: Optional[float] = 10.0):
        """Permanently stop the server and its dispatcher.

        Drains in-flight work by default so replies are not lost.
        """
        self.stop(drain=drain, drain_timeout=drain_timeout)
        self._request_timeouts.shutdown()
        self.dispatcher.shutdown()

    def _resolve_client_protocol(self, client_addr: tuple):
        with self._clients_lock:
            protocol = self._clients.get(client_addr)
        if not protocol:
            raise ExceptionType.NotConnected(f"Client {client_addr} not connected")
        if not getattr(protocol, "_authenticated", True):
            raise ExceptionType.AuthenticationError(
                f"Client {client_addr} is not authenticated"
            )
        if not getattr(protocol, "_handshake_completed", True):
            raise ExceptionType.HandshakeError(
                f"Client {client_addr} handshake is not complete"
            )
        return protocol

    def _encode_outbound(
        self,
        data: Any,
        data_id: Optional[str],
        path: Optional[str],
        wait_response: bool,
        status_code: int = 200,
    ):
        """Encode one outbound user message and return (data_id, frame)."""
        if data_id is None:
            data_id = str(uuid.uuid4())

        headers = {
            "type": "__user__",
            "id": data_id,
            "path": path,
            "wait_response": wait_response,
        }
        if status_code != 200:
            headers["status_code"] = status_code

        if self.compress:
            try:
                compressed_msg = MultiCompressor.compress(
                    data,
                    method=self.compression_type,
                    level=self.compression_level,
                    allow_pickle=self.allow_pickle,
                )
                headers["compressed"] = True
                length_bytes, encoded_message = message_manager.encode_with_length(
                    headers, compressed_msg
                )
            except Exception as e:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.CompressionError(
                            f"Compression failed: {e}"
                        ),
                        context="server.send_client",
                    ),
                )
                raise ExceptionType.CompressionError(f"Compression failed: {e}")
        else:
            length_bytes, encoded_message = message_manager.encode_with_length(
                headers, data
            )

        if len(encoded_message) > self.max_frame_size:
            raise ExceptionType.InvalidData(
                f"Outbound frame exceeds the maximum size of {self.max_frame_size} bytes"
            )

        return data_id, length_bytes + encoded_message

    def send_client_async(
        self,
        client_addr: tuple,
        data: Any,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        timeout: Optional[float] = 30.0,
        status_code: int = 200,
    ) -> RequestHandle:
        """Send a request to a client and return a handle without blocking.

        The handle exposes ``done()``, ``result()``, ``exception()``,
        ``cancel()`` and ``add_done_callback()``.
        """
        protocol = self._resolve_client_protocol(client_addr)
        data_id, frame = self._encode_outbound(
            data, data_id, path, True, status_code
        )

        # Register before sending because the peer can reply before send returns.
        request = RequestHandle(
            self.pending_responses,
            self._request_timeouts,
            data_id,
            client_addr,
            timeout,
            f"No response received within {timeout} timeout",
        )
        try:
            self.pending_responses.add(data_id, request, owner=client_addr)
        except ValueError:
            request.cancel()
            raise

        try:
            protocol.send(frame)
        except Exception:
            request.cancel()
            raise

        return request

    def send_client(
        self,
        client_addr: tuple,
        data: Any,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
        status_code: int = 200,
    ):
        """Send data to a client. Blocks only when wait_response is True."""
        if not wait_response:
            protocol = self._resolve_client_protocol(client_addr)
            data_id, frame = self._encode_outbound(
                data, data_id, path, False, status_code
            )
            protocol.send(frame)
            return None

        request = self.send_client_async(
            client_addr,
            data,
            data_id=data_id,
            path=path,
            timeout=wait_response_timeout,
            status_code=status_code,
        )
        try:
            return request.result()
        except concurrent.futures.CancelledError:
            raise ExceptionType.NoResponse(
                f"No response received within {wait_response_timeout} - request cancelled"
            )
        except ExceptionType.NotConnected:
            raise ExceptionType.NoResponse(
                f"No response received within {wait_response_timeout} - not connected"
            )
        except ExceptionType.NoResponse:
            raise ExceptionType.NoResponse(
                f"No response received within {wait_response_timeout}"
            )
        except Exception as e:
            raise ExceptionType.ClientError(f"Error while waiting for response: {e}")
        finally:
            request.cancel()

    def disconnect_client(self, client_addr: tuple):
        """Disconnect a specific client."""
        with self._clients_lock:
            protocol = self._clients.pop(client_addr, None)
        if protocol and self._event_loop and protocol._event_connection:
            self._event_loop.remove_connection(protocol._event_connection)
        elif protocol:
            if protocol.socket:
                try:
                    protocol.socket.shutdown(socket_module.SHUT_RDWR)
                except OSError:
                    pass
            protocol.handle_connection_lost()

    def wait(self):
        """Wait for server to run (blocking)"""
        try:
            while self._server:
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.stop()
        except Exception:
            self.stop()

    def start_and_wait(self):
        """Start server and wait (blocking)"""
        self.start()
        self.wait()

    def get_connected_clients(self):
        with self._clients_lock:
            return len(self._clients)

    @property
    def memory_used(self):
        return self.memory_budget.used_bytes

    @property
    def memory_limit(self):
        return self.memory_budget.max_bytes

    def is_connected(self, client_addr):
        with self._clients_lock:
            return client_addr in self._clients

    def get_client_identity(self, client_addr: tuple) -> Optional[str]:
        """Return the verified certificate common name for a client, or None."""
        with self._clients_lock:
            protocol = self._clients.get(client_addr)
        if protocol is None:
            return None
        return getattr(protocol, "client_identity", None)

    def event(self, event_type: str):
        return self.dispatcher.event(event_type)

    def path(self, path: str, middleware=None, block: bool = False):
        return self.dispatcher.path(path, middleware, block)

    def register_blueprint(self, blueprint):
        blueprint._server = self  # Associate blueprint with this server
        self.dispatcher.register_blueprint(blueprint)
