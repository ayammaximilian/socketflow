from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


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
    client_identity: Optional[str] = None


@dataclass
class ClientDisconnectData:
    client_addr: Tuple[str, int]
    transport: Any
    client_identity: Optional[str] = None


@dataclass
class MessageReceivedData:
    data: Any
    client_addr: Optional[Tuple[str, int]] = None
    server_addr: Optional[Tuple[str, int]] = None
    data_id: Optional[str] = None
    params: Dict[str, str] = field(default_factory=dict)
    direct_response: bool = False
    client_identity: Optional[str] = None
    status_code: int = 200


@dataclass
class ResponseData:
    data: Any
    data_id: Optional[str] = None
    status_code: int = 200


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


@dataclass
class ServerDrainData:
    host: str
    port: int
    connected_clients: int
    reason: str = "shutdown"


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
        DRAINING = "server.draining"

    class Global:
        ERROR = "global.error"
