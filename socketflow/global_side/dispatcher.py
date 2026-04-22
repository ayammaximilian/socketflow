from typing import Dict, List, Callable, Any
import threading


class EventDispatcher:
    def __init__(self):
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._path_handlers: Dict[str, List[Callable]] = {}
        self._path_middleware: Dict[str, List[Callable]] = {}
        self._server = None
        self._client = None

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

        # Register middleware separately if provided
        if middleware:
            if isinstance(middleware, list):
                for m in middleware:
                    self.register_path_middleware(path, m)
            else:
                self.register_path_middleware(path, middleware)

    def register_path_middleware(self, path: str, middleware: Callable):
        """Register path middleware"""
        if path not in self._path_middleware:
            self._path_middleware[path] = []
        self._path_middleware[path].append(middleware)

    def emit(self, event_type: str, data: Any):
        """Emit event"""

        def _run_handlers():
            if event_type in self._event_handlers:
                for handler in self._event_handlers[event_type]:
                    try:
                        handler(data)
                    except Exception:
                        pass

        threading.Thread(target=_run_handlers, daemon=True).start()

    def emit_path(self, path: str, data: Any):
        """Emit path event"""

        def _run_path_handlers():
            # Run middleware first
            current_data = data
            if path in self._path_middleware:
                for middleware_func in self._path_middleware[path]:
                    try:
                        result = middleware_func(current_data)
                        if result is False:  # Middleware rejected the request
                            return
                        elif result is not None:  # Middleware modified the data
                            current_data = result
                    except Exception:
                        # Middleware failed, don't proceed to handlers
                        return

            # Run path handlers
            if path in self._path_handlers:
                for handler in self._path_handlers[path]:
                    try:
                        handler(current_data)
                    except Exception:
                        pass

        threading.Thread(target=_run_path_handlers, daemon=True).start()

    def register_blueprint(self, blueprint):
        """Register a blueprint"""
        blueprint.register_with_dispatcher(self)

    def set_event_loop(self, loop):
        """Compatibility method - not needed for sync version"""
        pass
