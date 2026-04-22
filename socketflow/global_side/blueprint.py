from typing import Dict, List, Callable, Optional
from .exceptions import ExceptionType


class Blueprint:
    def __init__(self, name: str):
        self.name = name
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._path_handlers: Dict[str, List[Callable]] = {}
        self._path_middleware: Dict[str, List[Callable]] = {}

    def event(self, event_type: str):
        """Decorator for event handlers"""

        def decorator(func):
            self.register_event(event_type, func)
            return func

        return decorator

    def path(self, path: str, middleware=None):
        """Decorator for path handlers"""

        def decorator(func):
            self.register_path(path, func, middleware)
            return func

        return decorator

    def register_event(self, event_type: str, handler: Callable):
        """Register an event handler"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def register_path(self, path: str, handler: Callable, middleware=None):
        """Register a path handler"""
        if path not in self._path_handlers:
            self._path_handlers[path] = []
        self._path_handlers[path].append(handler)

        # Store middleware separately if provided
        if middleware:
            if path not in self._path_middleware:
                self._path_middleware[path] = []
            if isinstance(middleware, list):
                self._path_middleware[path].extend(middleware)
            else:
                self._path_middleware[path].append(middleware)

    def register_with_dispatcher(self, dispatcher):
        """Register all handlers with a dispatcher"""
        for event_type, handlers in self._event_handlers.items():
            for handler in handlers:
                dispatcher.register_event(event_type, handler)

        for path, handlers in self._path_handlers.items():
            for handler in handlers:
                # Get middleware for this path
                middleware = self._path_middleware.get(path, [])
                dispatcher.register_path(path, handler, middleware)

    def is_connected(self, client_addr: tuple = None):
        if hasattr(self, "_client") and self._client:
            return self._client.is_connected()
        elif hasattr(self, "_server") and self._server:
            if not client_addr:
                raise ExceptionType.BlueprintError("client_addr parameter is required")
            return self._server.is_connected(client_addr)
        else:
            raise ExceptionType.BlueprintError(
                "Blueprint not registered with client or server"
            )

    def send(
        self,
        data: bytes,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
    ):
        """Send message from client (for blueprints)"""
        if hasattr(self, "_client") and self._client:
            return self._client.send(
                data, data_id, path, wait_response, wait_response_timeout
            )
        raise ExceptionType.BlueprintError("Blueprint not registered with client")

    def send_client(
        self,
        client_addr: tuple,
        data: bytes,
        data_id: Optional[str] = None,
        path: Optional[str] = None,
        wait_response: bool = False,
        wait_response_timeout: Optional[float] = 30.0,
    ):
        """Send message to client (for blueprints)"""
        if hasattr(self, "_server") and self._server:
            return self._server.send_client(
                client_addr, data, data_id, path, wait_response, wait_response_timeout
            )
        raise ExceptionType.BlueprintError("Blueprint not registered with server")

    def disconnect(self):
        """Disconnect client (for blueprints)"""
        if hasattr(self, "_client") and self._client:
            self._client.disconnect()
        else:
            raise ExceptionType.BlueprintError("Blueprint not registered with client")

    def disconnect_client(self, client_addr: tuple):
        """Disconnect client (for blueprints)"""
        if hasattr(self, "_server") and self._server:
            self._server.disconnect_client(client_addr)
        else:
            raise ExceptionType.BlueprintError("Blueprint not registered with server")
