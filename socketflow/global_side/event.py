from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass
class ConnectData:
    server_addr: Tuple[str, int]
    transport: Any


@dataclass
class DisconnectData:
    server_addr: Tuple[str, int]
    transport: Any


@dataclass
class ClientConnectData:
    client_addr: Tuple[str, int]
    transport: Any


@dataclass
class ClientDisconnectData:
    client_addr: Tuple[str, int]
    transport: Any


@dataclass
class MessageReceivedData:
    data: Any
    client_addr: Optional[Tuple[str, int]] = None
    server_addr: Optional[Tuple[str, int]] = None
    data_id: Optional[str] = None


@dataclass
class ErrorData:
    error: Exception
    context: str


@dataclass
class ServerStartData:
    host: str
    port: int


@dataclass
class ServerStopData:
    host: str
    port: int


class EventType:
    class Client:
        CONNECT = "client.connect"
        DISCONNECT = "client.disconnect"
        MESSAGE = "client.message"

    class Server:
        CLIENT_CONNECT = "server.client_connect"
        CLIENT_DISCONNECT = "server.client_disconnect"
        MESSAGE = "server.message"
        START = "server.start"
        STOP = "server.stop"

    class Global:
        ERROR = "global.error"
