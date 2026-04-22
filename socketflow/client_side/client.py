import threading
import socket as socket_module
from ..global_side.event import (
    EventType,
    ConnectData,
    DisconnectData,
    MessageReceivedData,
    ErrorData,
)
from ..global_side.dispatcher import EventDispatcher
from ..global_side.compression import MultiCompressor
from ..global_side.message_manager import message_manager
from ..global_side.message_handler import message_handler
from ..global_side.exceptions import ExceptionType
from typing import Optional, Union
import uuid
import time
import concurrent.futures


class TcpClientProtocol:
    def __init__(self, client, socket):
        self.client = client
        self.socket = socket
        self._buffer = bytearray()
        self._last_ping_time = None
        self._missed_pings = 0
        self._ping_task = None

    def handle_data(self, data):
        """Handle incoming data from server"""
        self._buffer.extend(data)
        self._missed_pings = 0

        offset = 0
        while len(self._buffer) - offset >= 4:
            msg_len = int.from_bytes(self._buffer[offset : offset + 4], byteorder="big")

            if len(self._buffer) - offset < 4 + msg_len:
                break

            start = offset + 4
            end = start + msg_len
            message_data = self._buffer[start:end]

            offset = end

            headers, body = message_handler.unpack_data(bytes(message_data))
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
            elif msg_type == "__user__":
                path = headers.get("path")
                data_id = headers.get("id")
                server_addr = (
                    self.socket.getpeername() if self.socket else ("unknown", 0)
                )
                event_data = MessageReceivedData(
                    data=body, server_addr=server_addr, data_id=data_id
                )

                if data_id and data_id in self.client.pending_responses:
                    future = self.client.pending_responses.pop(data_id)
                    if hasattr(future, "set_result") and not future.done():
                        future.set_result(body)
                    continue

                if path:
                    self.client.dispatcher.emit_path(path, event_data)
                else:
                    self.client.dispatcher.emit(EventType.Client.MESSAGE, event_data)

        if offset > 0:
            del self._buffer[:offset]

    def send_data(self, data):
        """Send data to server"""
        if self.socket:
            try:
                self.socket.sendall(data)
            except Exception as e:
                raise ExceptionType.MessageHandlerError(e)
        else:
            raise ExceptionType.NotConnected("Not connected to server")

    def handle_connection_lost(self):
        """Handle server disconnection"""
        self._ping_task = None

        self.client._connected = False
        server_addr = self._server_addr if self._server_addr else ("unknown", 0)

        for data_id, future in list(self.client.pending_responses.items()):
            if hasattr(future, "set_exception") and not future.done():
                future.set_exception(
                    ExceptionType.NotConnected(f"Server {server_addr} disconnected")
                )
        self.client.pending_responses.clear()

        self.client.dispatcher.emit(
            EventType.Client.DISCONNECT,
            DisconnectData(server_addr=server_addr, transport=self.socket),
        )

        self.socket.close()

        self._connected = False

    def keepalive_check(self):
        """Send periodic pings to server"""
        while self.client._connected:
            try:
                ping_message = message_handler.create_ping()
                self.send_data(ping_message)
            except Exception:
                pass

            if not self.client._connected:
                break

            time.sleep(self.client.keepalive_interval)

            self._missed_pings += 1

            if self._missed_pings >= self.client.keepalive_max_missed:
                self.client.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.KeepaliveTimeout("Keepalive timeout"),
                        context="client.keepalive",
                    ),
                )
                self.handle_connection_lost()
                break


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
        flow_control: bool = True,
        recv_buffer_size: int = 65536,
        send_buffer_size: int = 65536,
    ):
        self.host = host
        self.port = port
        self.dispatcher = EventDispatcher()
        self._socket = None
        self._protocol = None
        self._connected = False
        self.compression_type = compression_type
        self.compression_level = compression_level
        self.compress = compress
        self.keepalive_interval = keepalive_interval
        self.keepalive_max_missed = keepalive_max_missed
        self.connection_timeout = connection_timeout
        self.flow_control_enabled = flow_control
        self.pending_responses = {}
        self.seperator = b"\r\nSOCKETFLOW\r\n"
        self.recv_buffer_size = recv_buffer_size
        self.send_buffer_size = send_buffer_size

    def connect(self):
        """Connect to server"""
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

            # Enable TCP Keep-Alive
            self._socket.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_KEEPALIVE, 1
            )

            self._socket.connect((self.host, self.port))
            self._socket.settimeout(None)

            self._protocol = TcpClientProtocol(self, self._socket)
            self._connected = True

            threading.Thread(target=self._receive_loop, daemon=True).start()

            threading.Thread(target=self._protocol.keepalive_check, daemon=True).start()

            server_addr = self._socket.getpeername()
            self.dispatcher.emit(
                EventType.Client.CONNECT,
                ConnectData(server_addr=server_addr, transport=self._socket),
            )
            # Store server address for later use
            self._protocol._server_addr = server_addr

        except socket_module.timeout:
            self.dispatcher.emit(
                EventType.Global.ERROR,
                ErrorData(
                    error=ExceptionType.ConnectionTimeout(
                        f"Connection timeout after {self.connection_timeout}s"
                    ),
                    context="client.connect",
                ),
            )
            raise ExceptionType.ConnectionTimeout(
                f"Connection timeout after {self.connection_timeout}s"
            )
        except Exception as e:
            self.dispatcher.emit(
                EventType.Global.ERROR, ErrorData(error=e, context="client.connect")
            )
            raise

    def _receive_loop(self):
        """Main receive loop"""
        while self._connected:
            try:
                data = self._socket.recv(65536)
                if not data:
                    break
                self._protocol.handle_data(data)
            except socket_module.timeout:
                pass
            except Exception:
                break
        self._protocol.handle_connection_lost()

    def disconnect(self):
        """Disconnect from server"""
        if self._connected:
            self._socket.close()
            self._connected = False

    def send(
        self,
        data: Union[bytes, str],
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
    ):
        if not self._connected:
            raise ExceptionType.NotConnected("Client is not connected")

        if data_id is None:
            data_id = str(uuid.uuid4())

        headers = {
            "type": "__user__",
            "id": data_id,
            "path": path,
        }

        if self.compress:
            try:
                compressed_msg = MultiCompressor.compress(
                    data, method=self.compression_type, level=self.compression_level
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

        self._protocol.send_data(length_bytes + encoded_message)

        if wait_response:
            future = concurrent.futures.Future()
            self.pending_responses[data_id] = future

            def timeout_handler():
                if data_id in self.pending_responses:
                    self.pending_responses.pop(data_id, None)
                    if not future.done():
                        future.set_exception(
                            ExceptionType.NoResponse(
                                f"No response received within {wait_response_timeout} timeout"
                            )
                        )

            threading.Timer(wait_response_timeout, timeout_handler).start()

            try:
                return future.result()
            except ExceptionType.NotConnected:
                raise ExceptionType.NoResponse(
                    f"No response received within {wait_response_timeout} timeout - not connected"
                )
            except ExceptionType.NoResponse:
                raise ExceptionType.NoResponse(
                    f"No response received within {wait_response_timeout} timeout"
                )
            except Exception as e:
                raise ExceptionType.ClientError(
                    f"Error while waiting for response: {e}"
                )
            finally:
                self.pending_responses.pop(data_id, None)

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

    def event(self, event_type: str):
        return self.dispatcher.event(event_type)

    def path(self, path: str, middleware=None):
        return self.dispatcher.path(path, middleware)

    def register_blueprint(self, blueprint):
        blueprint._client = self  # Associate blueprint with this client
        self.dispatcher.register_blueprint(blueprint)
