import threading
import socket as socket_module
from ..global_side.event import (
    EventType,
    ClientConnectData,
    ClientDisconnectData,
    MessageReceivedData,
    ErrorData,
    ServerStartData,
    ServerStopData,
)
from ..global_side.dispatcher import EventDispatcher
from ..global_side.compression import MultiCompressor
from ..global_side.message_manager import message_manager
from ..global_side.message_handler import message_handler
from ..global_side.exceptions import ExceptionType
from typing import Optional
import uuid
import time
import concurrent.futures


class TcpServerProtocol:
    def __init__(self, server):
        self.server = server
        self.socket = None
        self.client_addr = None
        self._buffer = bytearray()
        self._last_ping_time = None
        self._missed_pings = 0

    def handle_data(self, data):
        """Handle incoming data from client"""
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
            elif msg_type == "__user__":
                path = headers.get("path")
                data_id = headers.get("id")
                event_data = MessageReceivedData(
                    data=body, client_addr=self.client_addr, data_id=data_id
                )

                if data_id and data_id in self.server.pending_responses:
                    future = self.server.pending_responses.pop(data_id)
                    if hasattr(future, "set_result") and not future.done():
                        future.set_result(body)
                    continue

                if path:
                    self.server.dispatcher.emit_path(path, event_data)
                else:
                    self.server.dispatcher.emit(EventType.Server.MESSAGE, event_data)

        if offset > 0:
            del self._buffer[:offset]

    def send(self, data):
        """Send data to client"""
        if self.socket:
            try:
                self.socket.sendall(data)
            except Exception as e:
                raise ExceptionType.MessageHandlerError(e)
        else:
            raise ExceptionType.NotConnected("Not connected to client")

    def handle_connection_lost(self):
        """Handle client disconnection"""
        self.server._clients.pop(self.client_addr, None)

        for data_id, future in list(self.server.pending_responses.items()):
            if hasattr(future, "set_exception") and not future.done():
                future.set_exception(
                    ExceptionType.NotConnected(
                        f"Client {self.client_addr} disconnected"
                    )
                )
        self.server.pending_responses.clear()

        self.server.dispatcher.emit(
            EventType.Server.CLIENT_DISCONNECT,
            ClientDisconnectData(client_addr=self.client_addr, transport=self.socket),
        )

        if self.socket:
            self.socket.close()

        self.socket = None

    def keepalive_check(self):
        """Send periodic pings to client and check for missed pongs"""
        while self.client_addr in self.server._clients:
            try:
                ping_message = message_handler.create_ping()
                self.send(ping_message)
            except Exception:
                pass

            if self.client_addr not in self.server._clients:
                break

            time.sleep(self.server.keepalive_interval)

            self._missed_pings += 1

            if self._missed_pings >= self.server.keepalive_max_missed:
                self.server.dispatcher.emit(
                    EventType.Global.ERROR,
                    ErrorData(
                        error=ExceptionType.KeepaliveTimeout("Keepalive timeout"),
                        context="server.keepalive",
                    ),
                )
                self.handle_connection_lost()
                break


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
        flow_control: bool = True,
        recv_buffer_size: int = 65536,
        send_buffer_size: int = 65536,
    ):
        self.host = host
        self.port = port
        self.dispatcher = EventDispatcher()
        self._server = None
        self._clients = {}
        self.compression_type = compression_type
        self.compression_level = compression_level
        self.compress = compress
        self.keepalive_interval = keepalive_interval
        self.keepalive_max_missed = keepalive_max_missed
        self.flow_control_enabled = flow_control
        self.pending_responses = {}
        self.seperator = b"\r\nSOCKETFLOW\r\n"
        self.recv_buffer_size = recv_buffer_size
        self.send_buffer_size = send_buffer_size

    def _handle_connection(self, client_socket, address):
        protocol = TcpServerProtocol(self)
        protocol.socket = client_socket
        protocol.client_addr = address
        self._clients[address] = protocol

        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_RCVBUF, self.recv_buffer_size
        )
        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, self.send_buffer_size
        )

        client_socket.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_KEEPALIVE, 1
        )

        threading.Thread(target=protocol.keepalive_check, daemon=True).start()

        self.dispatcher.emit(
            EventType.Server.CLIENT_CONNECT,
            ClientConnectData(client_addr=address, transport=client_socket),
        )

        while True:
            try:
                data = client_socket.recv(65536)
                if not data:
                    break
                protocol.handle_data(data)
            except socket_module.timeout:
                pass
            except Exception:
                break
        protocol.handle_connection_lost()

    def start(self):
        """Start the server"""
        try:
            self._server = socket_module.socket(
                socket_module.AF_INET, socket_module.SOCK_STREAM
            )
            self._server.setsockopt(
                socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1
            )
            self._server.bind((self.host, self.port))
            self._server.listen(100)

            self.dispatcher.emit(
                EventType.Server.START, ServerStartData(host=self.host, port=self.port)
            )

            # Start accepting connections in a separate thread
            threading.Thread(target=self._accept_connections, daemon=True).start()

        except Exception as e:
            self.dispatcher.emit(
                EventType.Global.ERROR, ErrorData(error=e, context="server.start")
            )
            raise

    def _accept_connections(self):
        """Accept incoming connections"""
        while self._server:  # Check if server is still running
            try:
                client_socket, address = self._server.accept()
                threading.Thread(
                    target=self._handle_connection,
                    args=(client_socket, address),
                    daemon=True,
                ).start()
            except Exception as e:
                if not self._server:  # Server was stopped
                    break
                self.dispatcher.emit(
                    EventType.Global.ERROR, ErrorData(error=e, context="server.accept")
                )

    def stop(self):
        """Stop the server"""
        if self._server:
            self._server.close()
            self._server = None

        # Disconnect all existing clients
        for client_addr, protocol in list(self._clients.items()):
            if protocol.socket:
                protocol.socket.close()
        self._clients.clear()

        self.dispatcher.emit(
            EventType.Server.STOP, ServerStopData(host=self.host, port=self.port)
        )

    def send_client(
        self,
        client_addr: tuple,
        data: bytes,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
    ):
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
                        context="server.send_client",
                    ),
                )
                raise ExceptionType.CompressionError(f"Compression failed: {e}")
        else:
            length_bytes, encoded_message = message_manager.encode_with_length(
                headers, data
            )

        protocol = self._clients.get(client_addr)
        if not protocol:
            raise ExceptionType.NotConnected(f"Client {client_addr} not connected")

        protocol.send(length_bytes + encoded_message)
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

    def disconnect_client(self, client_addr: tuple):
        """Disconnect a specific client"""
        protocol = self._clients.pop(client_addr, None)
        if protocol and protocol.socket:
            protocol.socket.close()

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
        return len(self._clients)

    def is_connected(self, client_addr):
        return client_addr in self._clients

    def event(self, event_type: str):
        return self.dispatcher.event(event_type)

    def path(self, path: str, middleware=None):
        return self.dispatcher.path(path, middleware)

    def register_blueprint(self, blueprint):
        blueprint._server = self  # Associate blueprint with this server
        self.dispatcher.register_blueprint(blueprint)
