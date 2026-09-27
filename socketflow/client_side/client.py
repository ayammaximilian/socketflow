import select
import ssl
import threading
import socket as socket_module
from ..global_side.event import (
    EventType,
    ConnectData,
    DisconnectData,
    MessageReceivedData,
    ResponseData,
    ErrorData,
)
from ..global_side.dispatcher import EventDispatcher
from ..global_side.compression import MultiCompressor
from ..global_side.message_manager import message_manager
from ..global_side import logs
from ..global_side import metrics as metrics_module
from ..global_side import protocol as protocol_module
from ..global_side.message_handler import message_handler
from ..global_side.transport import (
    MemoryBudget,
    PendingResponseRegistry,
    RequestHandle,
    SocketWriter,
    TimeoutScheduler,
)
from ..global_side.exceptions import ExceptionType
from typing import Any, Optional, Union
import uuid
import time
import concurrent.futures


class TcpClientProtocol:
    def __init__(
        self,
        client,
        socket,
        max_frame_size=8 * 1024 * 1024,
        max_queue_bytes=16 * 1024 * 1024,
        max_pending_writes=1000,
        allow_pickle=False,
        memory_budget=None,
        max_decompressed_size=16 * 1024 * 1024,
    ):
        self.client = client
        self.socket = socket
        self.max_frame_size = max_frame_size
        self.max_decompressed_size = max_decompressed_size
        self.allow_pickle = allow_pickle
        self._buffer = bytearray()
        self._memory_budget = memory_budget
        self._inbound_reserved = 0
        self._inbound_lock = threading.Lock()
        self._last_ping_time = time.monotonic()
        self._last_received_at = time.monotonic()
        self._last_ping_at = time.monotonic()
        self._missed_pings = 0
        self._ping_task = None
        self._connection_lost = threading.Event()
        self._control_lock = threading.Lock()
        self._writer = SocketWriter(
            socket,
            max_queue_bytes=max_queue_bytes,
            max_pending_writes=max_pending_writes,
            name="socketflow-client-writer",
            memory_budget=memory_budget,
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

    def handle_data(self, data):
        """Handle incoming data from server"""
        self._reserve_inbound(len(data))
        self._buffer.extend(data)
        self._last_received_at = time.monotonic()
        self._missed_pings = 0

        offset = 0
        while len(self._buffer) - offset >= 4:
            msg_len = int.from_bytes(self._buffer[offset : offset + 4], byteorder="big")
            if msg_len > self.max_frame_size:
                error = ExceptionType.InvalidData(
                    f"Frame exceeds the maximum size of {self.max_frame_size} bytes"
                )
                self.client.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=error, context="client.handle_data"),
                )
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
                self.client.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.InvalidData(error_msg),
                        context="client.handle_data",
                    ),
                )
                continue

            msg_type = headers.get("type")
            if not msg_type:
                self.client.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.InvalidData(
                            "Received message without type"
                        ),
                        context="client.handle_data",
                    ),
                )
                continue

            if msg_type == "__ping__":
                pong_message = message_handler.create_pong()
                try:
                    self.send_data(pong_message)
                except Exception:
                    pass
            elif msg_type == "__server_ready__":
                self.client._on_server_ready(body)
                continue
            elif msg_type == "__protocol_error__":
                self.client._on_protocol_error(body)
                continue
            elif msg_type == "__auth_ack__":
                if not isinstance(body, dict) or not body.get("ok", False):
                    self.client._auth_error = ExceptionType.HandshakeError(
                        str(body.get("error", "Authentication failed"))
                        if isinstance(body, dict)
                        else "Authentication failed"
                    )
                self.client._auth_ack_event.set()
                if self.client._auth_error is not None or not self.client.require_handshake:
                    self.client._auth_event.set()
                continue
            elif msg_type == "__handshake_ok__":
                self.client._handshake_completed = True
                self.client._auth_event.set()
                continue
            elif msg_type == "__user__":
                path = headers.get("path")
                data_id = headers.get("id")
                server_addr = self.client._server_addr
                if not server_addr:
                    server_addr = (
                        self.socket.getpeername()
                        if self.socket
                        else ("unknown", 0)
                    )
                wait_response = headers.get("wait_response", False)
                status_code = headers.get("status_code", 200)
                self.client.metrics.increment(
                    "messages_received_total", path=path or "none"
                )
                event_data = MessageReceivedData(
                    data=body,
                    server_addr=server_addr,
                    data_id=data_id,
                    direct_response=wait_response,
                    status_code=status_code,
                )

                # Resolve pending wait_response futures
                future = (
                    self.client.pending_responses.pop(data_id) if data_id else None
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
                    self.client.dispatcher.emit_path(path, event_data)
                else:
                    self.client.dispatcher.emit(EventType.Client.MESSAGE, event_data)

        if offset > 0:
            try:
                del self._buffer[:offset]
            finally:
                self._release_inbound(offset)

    def send_data(self, data):
        """Queue data for serialized delivery to the server."""
        if not self.socket:
            raise ExceptionType.NotConnected("Not connected to server")
        self._writer.send(data)

    def send_control(self, data):
        """Send a control frame before the normal reader starts."""
        if not self.socket:
            raise ExceptionType.NotConnected("Not connected to server")
        with self._control_lock:
            self.socket.sendall(data)

    def close(self):
        self._writer.abort()

    def handle_connection_lost(self):
        """Handle server disconnection exactly once."""
        if self._connection_lost.is_set():
            return
        self._connection_lost.set()
        self._ping_task = None
        self._release_all_inbound()
        self.client._connected = False

        server_addr = getattr(self, "_server_addr", None) or ("unknown", 0)
        failed = 0
        for future in self.client.pending_responses.drain():
            if not future.done():
                failed += 1
                future.set_exception(
                    ExceptionType.NotConnected(f"Server {server_addr} disconnected")
                )
        self.client.metrics.gauge("connected", 0)
        self.client._log.warning(
            "disconnected from server",
            server_addr=server_addr,
            failed_requests=failed,
        )

        try:
            self.client.dispatcher.emit(
                EventType.Client.DISCONNECT,
                DisconnectData(server_addr=server_addr, transport=self.socket),
            )
        except Exception:
            pass
        finally:
            self._writer.abort()
            if self.socket:
                self.socket.close()
            self.socket = None

    def keepalive_tick(self):
        """Send a keepalive probe when the reader polling interval expires."""
        interval = max(float(self.client.keepalive_interval), 0.1)
        now = time.monotonic()
        if now - self._last_ping_at < interval:
            return

        self._last_ping_at = now
        try:
            self.send_data(message_handler.create_ping())
        except Exception:
            return

        if now - self._last_received_at >= interval * max(
            self.client.keepalive_max_missed, 1
        ):
            try:
                self.client.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.KeepaliveTimeout("Keepalive timeout"),
                        context="client.keepalive",
                    ),
                )
            except Exception:
                pass
            self.handle_connection_lost()

    def keepalive_check(self):
        """Compatibility entry point for applications that start keepalive manually."""
        while self.client._connected:
            self.keepalive_tick()
            time.sleep(min(1.0, max(float(self.client.keepalive_interval), 0.1)))


class TcpClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        compression_type: str = "zlib",
        compression_level: int = 6,
        compress: bool = True,
        keepalive_interval: float = 30.0,
        keepalive_max_missed: int = 3,
        connection_timeout: float = 10.0,
        recv_buffer_size: int = 65536,
        send_buffer_size: int = 65536,
        max_frame_size: int = 8 * 1024 * 1024,
        max_outbound_queue_bytes: int = 16 * 1024 * 1024,
        max_pending_writes: int = 1000,
        max_dispatch_workers: int = 32,
        max_pending_tasks: int = 1000,
        dispatch_queue_timeout: float = 1.0,
        allow_pickle: bool = False,
        tls_enabled: bool = False,
        tls_ca_certs: Optional[str] = None,
        tls_server_hostname: Optional[str] = None,
        tls_certfile: Optional[str] = None,
        tls_keyfile: Optional[str] = None,
        auth_enabled: bool = False,
        auth_token: Optional[str] = None,
        auth_username: Optional[str] = None,
        auth_password: Optional[str] = None,
        auth_timeout: float = 30.0,
        require_handshake: bool = True,
        handshake_timeout: Optional[float] = None,
        allow_legacy_server: bool = False,
        legacy_probe_timeout: float = 1.0,
        max_memory_bytes: int = 64 * 1024 * 1024,
        max_decompressed_size: int = 16 * 1024 * 1024,
        protocol_version: Optional[int] = None,
        min_protocol_version: Optional[int] = None,
        max_protocol_version: Optional[int] = None,
    ):
        if max_frame_size < 1 or max_outbound_queue_bytes < 1 or max_pending_writes < 1:
            raise ValueError("Client transport limits must be positive")
        if max_memory_bytes < 1:
            raise ValueError("max_memory_bytes must be positive")
        if max_decompressed_size < 1:
            raise ValueError("max_decompressed_size must be positive")
        if auth_timeout <= 0:
            raise ValueError("auth_timeout must be positive")
        if handshake_timeout is not None and handshake_timeout <= 0:
            raise ValueError("handshake_timeout must be positive")
        if (auth_username is None) != (auth_password is None):
            raise ValueError("auth_username and auth_password must be provided together")
        if auth_token is not None and auth_username is not None:
            raise ValueError("Use either auth_token or username/password authentication")
        if min_protocol_version is not None and max_protocol_version is not None:
            if min_protocol_version > max_protocol_version:
                raise ValueError(
                    "min_protocol_version cannot exceed max_protocol_version"
                )
        if (tls_certfile is None) != (tls_keyfile is None):
            raise ValueError(
                "tls_certfile and tls_keyfile must be provided together"
            )

        self.host = host
        self.port = port
        self.metrics = metrics_module.MetricsRegistry()
        self._log = logs.get_logger("socketflow.client")
        self.metrics.describe("messages_sent_total", "Messages sent to the server")
        self.metrics.describe("messages_received_total", "Messages received from the server")
        self.metrics.describe("bytes_sent_total", "Bytes written to the server")
        self.metrics.describe("errors_total", "Errors by context")
        self.metrics.describe("backpressure_total", "Rejections caused by full queues")
        self.metrics.describe("request_duration_seconds", "Time waiting for a reply")
        self.metrics.gauge("connected", 0)
        self.dispatcher = EventDispatcher(
            max_workers=max_dispatch_workers,
            max_pending_tasks=max_pending_tasks,
            queue_timeout=dispatch_queue_timeout,
            owner=self,
        )
        self._socket = None
        self._protocol = None
        self._server_addr = None
        self._reader_thread = None
        self._connected = False
        self.compression_type = compression_type
        self.compression_level = compression_level
        self.compress = compress
        self.keepalive_interval = keepalive_interval
        self.keepalive_max_missed = keepalive_max_missed
        self.connection_timeout = connection_timeout
        self.pending_responses = PendingResponseRegistry()
        self._request_timeouts = TimeoutScheduler()
        self.memory_budget = MemoryBudget(max_memory_bytes)
        self.recv_buffer_size = recv_buffer_size
        self.send_buffer_size = send_buffer_size
        self.max_frame_size = max_frame_size
        self.max_outbound_queue_bytes = max_outbound_queue_bytes
        self.max_pending_writes = max_pending_writes
        self.max_decompressed_size = max_decompressed_size
        self.allow_pickle = allow_pickle
        self.tls_enabled = tls_enabled
        self.tls_ca_certs = tls_ca_certs
        self.tls_server_hostname = tls_server_hostname
        self.tls_certfile = tls_certfile
        self.tls_keyfile = tls_keyfile
        self.auth_enabled = (
            require_handshake
            or auth_enabled
            or auth_token is not None
            or auth_username is not None
        )
        self.require_handshake = require_handshake
        self.allow_legacy_server = allow_legacy_server
        # 0.1.4 servers pickle their payloads and never send a handshake, so a
        # legacy peer needs pickle in both directions. Until the peer is
        # identified we assume legacy only because the flag asked us to; a
        # modern server downgrades this back to the safe JSON format.
        self.legacy_probe_timeout = legacy_probe_timeout
        self._pickle_outbound = allow_pickle or allow_legacy_server
        self._accept_pickle = allow_pickle or allow_legacy_server
        self._handshake_completed = False
        self.auth_token = auth_token
        self.auth_username = auth_username
        self.auth_password = auth_password
        self.auth_timeout = auth_timeout
        self.handshake_timeout = (
            connection_timeout if handshake_timeout is None else handshake_timeout
        )
        self._auth_event = threading.Event()
        self._auth_ack_event = threading.Event()
        self._server_ready_event = threading.Event()
        self._auth_error = None
        self.protocol_version = protocol_version
        self._protocol_enabled = protocol_module.is_enabled(
            protocol_version, min_protocol_version, max_protocol_version
        )
        self.protocol_versions = protocol_module.make_range(
            min_protocol_version, max_protocol_version
        )
        self._negotiated_version = None

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

    def _create_tls_context(self):
        if not self.tls_enabled:
            return None
        try:
            context = ssl.create_default_context(cafile=self.tls_ca_certs)
            if hasattr(context, "minimum_version") and hasattr(ssl, "TLSVersion"):
                context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            if self.tls_certfile and self.tls_keyfile:
                # Mutual TLS: present our certificate to the server.
                context.load_cert_chain(self.tls_certfile, self.tls_keyfile)
            elif self.tls_certfile or self.tls_keyfile:
                raise ValueError(
                    "tls_certfile and tls_keyfile must be provided together"
                )
            return context
        except ValueError:
            raise
        except (OSError, ssl.SSLError) as error:
            raise ExceptionType.TlsError(f"TLS setup failed: {error}")

    def _on_server_ready(self, body):
        """Record the server's protocol range and finish the ready wait."""
        if self._protocol_enabled and isinstance(body, dict):
            remote = body.get("protocol")
            agreed = protocol_module.negotiate(self.protocol_versions, remote)
            if agreed is None:
                self._auth_error = ExceptionType.ProtocolVersionError(
                    protocol_module.describe(self.protocol_versions, remote)
                )
                self._server_ready_event.set()
                self._auth_event.set()
                return
            self._negotiated_version = agreed
        self._server_ready_event.set()
    def _on_protocol_error(self, body):
        """Record a server-side version refusal."""
        reason = "Server refused the protocol version"
        if isinstance(body, dict):
            reason = str(body.get("error", reason))
        self._auth_error = ExceptionType.ProtocolVersionError(reason)
        self._server_ready_event.set()
        self._auth_ack_event.set()
        self._auth_event.set()

    def _wait_for_server_ready(self):
        if not self._server_ready_event.wait(self.handshake_timeout):
            raise ExceptionType.HandshakeError(self._ready_timeout_message())
        if self._auth_error is not None:
            raise self._auth_error

    def _probe_for_legacy_server(self) -> bool:
        """Report whether the peer looks like a pre-0.2 (0.1.4) server.

        A modern server announces itself with ``__server_ready__`` as soon as
        the connection is accepted; a 0.1.4 server has no handshake at all and
        sends nothing. A short grace period is therefore enough to tell them
        apart, and it costs a legacy connection only a one-time connect delay.
        """
        grace = min(float(self.legacy_probe_timeout), float(self.handshake_timeout))
        if grace <= 0:
            return True
        return not self._server_ready_event.wait(grace)

    def _ready_timeout_message(self) -> str:
        """Explain a failed handshake in terms the user can act on."""
        if self.tls_enabled and self.tls_certfile and self.tls_keyfile:
            return (
                "Server did not complete the TLS handshake. The server may have "
                "rejected your client certificate; check that it was issued by "
                "the server's tls_client_ca and is still valid."
            )
        if self.tls_enabled:
            return (
                "Server did not complete the TLS handshake. If the server "
                "requires a client certificate (tls_require_client_cert), set "
                "tls_certfile and tls_keyfile on the client."
            )
        return "Server did not complete the connection handshake"

    def _send_authentication(self):
        if not self.auth_enabled:
            return

        credentials = {}
        if self.auth_token is not None:
            credentials["token"] = self.auth_token
        if self.auth_username is not None:
            credentials["username"] = self.auth_username
        if self.auth_password is not None:
            credentials["password"] = self.auth_password
        if self._protocol_enabled:
            credentials["protocol"] = self.protocol_versions
            credentials["protocol_version"] = self.protocol_version

        headers = {"type": "__auth__"}
        length_bytes, encoded_message = message_manager.encode_with_length(
            headers, credentials
        )
        self._protocol.send_control(length_bytes + encoded_message)

    def _wait_for_authentication(self):
        if not self.require_handshake and not self.auth_enabled:
            self._handshake_completed = True
            return
        if self.require_handshake:
            if not self._auth_event.wait(self.handshake_timeout):
                raise ExceptionType.HandshakeError("Connection handshake timed out")
            if self._auth_error is not None:
                raise self._auth_error
            if not self._handshake_completed:
                raise ExceptionType.HandshakeError(
                    "Server did not complete the connection handshake"
                )
            return
        if not self._auth_ack_event.wait(self.handshake_timeout):
            raise ExceptionType.HandshakeError("Authentication timed out")
        if self._auth_error is not None:
            raise self._auth_error
        self._handshake_completed = True

    def _authenticate(self):
        self._send_authentication()
        self._wait_for_authentication()

    def _cleanup_transport(self):
        protocol = self._protocol
        if protocol:
            protocol.close()
        if self._socket:
            try:
                self._socket.shutdown(socket_module.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        self._socket = None
        self._protocol = None
        self._connected = False



    def connect(self):
        """Connect to server"""
        if self._connected:
            raise ExceptionType.NotConnected("Client is already connected")
        try:
            self._socket = socket_module.socket(
                socket_module.AF_INET, socket_module.SOCK_STREAM
            )
            self._socket.settimeout(self.connection_timeout)

            # Set socket buffer sizes
            self._socket.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_RCVBUF, self.recv_buffer_size
            )
            self._socket.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, self.send_buffer_size
            )

            # Enable TCP Keep-Alive and low-latency small writes.
            self._socket.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_KEEPALIVE, 1
            )
            try:
                self._socket.setsockopt(
                    socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1
                )
            except (AttributeError, OSError):
                pass

            self._socket.connect((self.host, self.port))
            if self.tls_enabled:
                try:
                    context = self._create_tls_context()
                    self._socket = context.wrap_socket(
                        self._socket,
                        server_hostname=self.tls_server_hostname or self.host,
                    )
                except (ssl.SSLError, OSError) as error:
                    raise ExceptionType.TlsError(f"TLS handshake failed: {error}")
            self._socket.settimeout(None)
            server_addr = self._socket.getpeername()

            self._auth_event.clear()
            self._auth_ack_event.clear()
            self._server_ready_event.clear()
            self._auth_error = None
            self._handshake_completed = False
            self._negotiated_version = None
            self._protocol = TcpClientProtocol(
                self,
                self._socket,
                max_frame_size=self.max_frame_size,
                max_queue_bytes=self.max_outbound_queue_bytes,
                max_pending_writes=self.max_pending_writes,
                allow_pickle=self._accept_pickle,
                memory_budget=self.memory_budget,
                max_decompressed_size=self.max_decompressed_size,
            )
            self._protocol._server_addr = server_addr
            self._connected = True

            self._reader_thread = threading.Thread(
                target=self._receive_loop,
                name="socketflow-client-reader",
                daemon=True,
            )
            self._reader_thread.start()
            if self.allow_legacy_server:
                if self._probe_for_legacy_server():
                    # Genuine 0.1.x peer: no handshake exists, and only pickle
                    # payloads are understood.
                    self._pickle_outbound = True
                    self._handshake_completed = True
                else:
                    # The peer speaks the current protocol after all, so send
                    # it the safe JSON format and run the normal handshake.
                    self._pickle_outbound = self.allow_pickle
                    if self.require_handshake:
                        if self.auth_enabled:
                            self._send_authentication()
                        self._wait_for_authentication()
                    else:
                        self._handshake_completed = True
            else:
                if self.require_handshake:
                    self._wait_for_server_ready()
                if self.auth_enabled:
                    self._send_authentication()
                if self.require_handshake:
                    self._wait_for_authentication()
                elif self.auth_enabled:
                    self._wait_for_authentication()

            self.dispatcher.emit(
                EventType.Client.CONNECT,
                ConnectData(server_addr=server_addr, transport=self._socket),
            )
            self.metrics.gauge("connected", 1)
            self._log.info(
                "connected to server",
                host=self.host,
                port=self.port,
                tls=self.tls_enabled,
                client_cert=bool(self.tls_certfile),
                protocol_version=self._negotiated_version,
            )

        except socket_module.timeout:
            self._cleanup_transport()
            try:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.ConnectionTimeout(
                            f"Connection timeout after {self.connection_timeout}s"
                        ),
                        context="client.connect",
                    ),
                )
            except Exception:
                pass
            raise ExceptionType.ConnectionTimeout(
                f"Connection timeout after {self.connection_timeout}s"
            )
        except (
            ExceptionType.TlsError,
            ExceptionType.AuthenticationError,
            ExceptionType.ProtocolVersionError,
        ) as e:
            self._cleanup_transport()
            try:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=e, context="client.connect"),
                )
            except Exception:
                pass
            raise
        except Exception as e:
            self._cleanup_transport()
            try:
                self.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(error=e, context="client.connect"),
                )
            except Exception:
                pass
            raise ExceptionType.ConnectionError(f"Connection error: {str(e)}")

    def _receive_loop(self):
        """Main blocking receive loop."""
        protocol = self._protocol
        while self._connected and self._socket:
            try:
                pending = getattr(self._socket, "pending", None)
                if pending is not None and pending():
                    data = self._socket.recv(65536)
                else:
                    readable, _, _ = select.select([self._socket], [], [], 1.0)
                    if not readable:
                        protocol.keepalive_tick()
                        continue
                    data = self._socket.recv(65536)
                if not data:
                    break
                protocol.handle_data(data)
            except socket_module.timeout:
                protocol.keepalive_tick()
            except Exception as error:
                try:
                    self.dispatcher.emit(
                        EventType.Global.ERROR,
                        ErrorData(error=error, context="client.receive"),
                    )
                    self._record_error("client.receive", error)
                except Exception:
                    pass
                break
        if protocol:
            protocol.handle_connection_lost()

    def disconnect(self):
        """Disconnect from server and fail pending requests."""
        protocol = self._protocol
        if protocol:
            protocol.handle_connection_lost()
        self._cleanup_transport()

        current_thread = threading.current_thread()
        for thread in (self._reader_thread,):
            if thread and thread is not current_thread:
                thread.join(timeout=1)
        self._reader_thread = None

    def shutdown(self):
        """Permanently close the client and its dispatcher."""
        self.disconnect()
        self._request_timeouts.shutdown()
        self.dispatcher.shutdown()

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
                    allow_pickle=self._pickle_outbound,
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
                        context="client.send",
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

    def _record_send(self, path: Optional[str], frame: bytes):
        """Count one outbound message and note it in the log."""
        self.metrics.increment("messages_sent_total", path=path or "none")
        self.metrics.increment("bytes_sent_total", len(frame))
        self._log.debug("message sent", path=path, bytes=len(frame))

    def send_async(
        self,
        data: Any,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        timeout: Optional[float] = 30.0,
        status_code: int = 200,
    ) -> RequestHandle:
        """Send a request and return a handle without blocking.

        The handle exposes ``done()``, ``result()``, ``exception()``,
        ``cancel()`` and ``add_done_callback()``.
        """
        if not self._connected:
            raise ExceptionType.NotConnected("Client is not connected")
        if self.require_handshake and not self._handshake_completed:
            raise ExceptionType.HandshakeError(
                "Connection handshake is not complete"
            )

        data_id, frame = self._encode_outbound(
            data, data_id, path, True, status_code
        )
        self._record_send(path, frame)

        # Register before sending because the peer can reply before send returns.
        request = RequestHandle(
            self.pending_responses,
            self._request_timeouts,
            data_id,
            None,
            timeout,
            f"No response received within {timeout} timeout",
        )
        try:
            self.pending_responses.add(data_id, request)
        except ValueError:
            request.cancel()
            raise

        try:
            self._protocol.send_data(frame)
        except Exception:
            request.cancel()
            raise

        return request

    def send(
        self,
        data: Any,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
        status_code: int = 200,
    ):
        """Send a message. Blocks only when wait_response is True."""
        if not wait_response:
            if not self._connected:
                raise ExceptionType.NotConnected("Client is not connected")
            if self.require_handshake and not self._handshake_completed:
                raise ExceptionType.HandshakeError(
                    "Connection handshake is not complete"
                )
            data_id, frame = self._encode_outbound(
                data, data_id, path, False, status_code
            )
            self._record_send(path, frame)
            self._protocol.send_data(frame)
            return None

        request = self.send_async(
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
            raise ExceptionType.ClientError(
                f"Error while waiting for response: {e}"
            )
        finally:
            request.cancel()

    def wait(self):
        """Wait for client to stay connected"""
        try:
            while self._connected:
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.disconnect()

    def connect_and_wait(self):
        """Connect and wait"""
        self.connect()
        self.wait()

    def is_connected(self):
        return self._connected

    @property
    def handshake_completed(self):
        return self._handshake_completed

    @property
    def negotiated_protocol_version(self):
        """Protocol version agreed with the server, or None if not negotiated."""
        return self._negotiated_version

    @property
    def memory_used(self):
        return self.memory_budget.used_bytes

    @property
    def memory_limit(self):
        return self.memory_budget.max_bytes

    def event(self, event_type: str):
        return self.dispatcher.event(event_type)

    def path(self, path: str, middleware=None, block: bool = False):
        return self.dispatcher.path(path, middleware, block)

    def register_blueprint(self, blueprint):
        blueprint._client = self
        self.dispatcher.register_blueprint(blueprint)
