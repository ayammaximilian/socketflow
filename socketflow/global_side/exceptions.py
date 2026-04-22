"""
Exception types for socketflow library
"""


class SocketFlowException(Exception):
    """Base exception for all socketflow exceptions"""

    pass


class NotConnected(SocketFlowException):
    """Raised when trying to perform operations on a disconnected client/server"""

    pass


class NoResponse(SocketFlowException):
    """Raised when no response is received within timeout"""

    pass


class ConnectionTimeout(SocketFlowException):
    """Raised when connection times out"""

    pass


class KeepaliveTimeout(SocketFlowException):
    """Raised when keepalive timeout occurs"""

    pass


class InvalidData(SocketFlowException):
    """Raised when invalid data is received or sent"""

    pass


class ProtocolError(SocketFlowException):
    """Raised when protocol error occurs"""

    pass


class ServerError(SocketFlowException):
    """Raised when server-side error occurs"""

    pass


class ClientError(SocketFlowException):
    """Raised when client-side error occurs"""

    pass


class BlueprintError(SocketFlowException):
    """Raised when blueprint-related error occurs"""

    pass


class CompressionError(SocketFlowException):
    """Raised when compression/decompression fails"""

    pass


class MessageHandlerError(SocketFlowException):
    """Raised when message handling fails"""

    pass


class DispatcherError(SocketFlowException):
    """Raised when dispatcher error occurs"""

    pass


# Namespace for grouped exception access
class ExceptionType:
    """Namespace for all exception types"""

    SocketFlow = SocketFlowException
    NotConnected = NotConnected
    NoResponse = NoResponse
    ConnectionTimeout = ConnectionTimeout
    KeepaliveTimeout = KeepaliveTimeout
    InvalidData = InvalidData
    ProtocolError = ProtocolError
    ServerError = ServerError
    ClientError = ClientError
    BlueprintError = BlueprintError
    CompressionError = CompressionError
    MessageHandlerError = MessageHandlerError
    DispatcherError = DispatcherError
