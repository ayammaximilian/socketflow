# SocketFlow

A high-performance, dependency-free TCP networking library for Python with advanced features like compression, event handling, bidirectional keepalive, and more.

## Features

- **Zero Dependencies** - Uses only Python's standard library
- **Bidirectional Keepalive** - Both client and server independently monitor connection health
- **TCP-Level Keepalive** - OS-managed keepalive for reliable connection detection
- **Compression** - Support for zlib, lzma, and bz2 compression
- **Event-Driven Architecture** - Flexible event dispatcher for handling server/client events
- **Blueprint System** - Organize your code with reusable blueprints
- **Middleware Support** - Add custom middleware to request/response processing
- **Path-Based Routing** - Route messages to specific handlers using paths
- **Efficient Buffer Handling** - O(N) buffer processing with offset pattern
- **Type Hints** - Full type annotations for better IDE support
- **Cross-Platform** - Works on Windows, Linux, and macOS

## Installation

```bash
pip install socketflow
```

**Full documentation available at:** https://socketflow.dev/

Or install from source:

```bash
git clone https://github.com/ayammaximilian/socketflow.git
cd socketflow
pip install .
```

## Quick Start

### Server Example

```python
from socketflow import TcpServer, EventType

# Create server
server = TcpServer(
    host="127.0.0.1",
    port=8080,
    keepalive_interval=30.0,
    keepalive_max_missed=3,
    compress=True
)

# Register event handler
@server.event(EventType.Server.MESSAGE)
def handle_message(data):
    print(f"Received: {data}")
    return "Response"

# Start server
server.start()
server.wait()  # Keep server running
```

### Client Example

```python
from socketflow import TcpClient, EventType

# Create client
client = TcpClient(
    host="127.0.0.1",
    port=8080,
    keepalive_interval=30.0,
    keepalive_max_missed=3,
    compress=True
)

# Connect to server
client.connect()

# Register event handler
@client.event(EventType.Client.MESSAGE)
def handle_message(data):
    print(f"Received: {data}")

# Send message
response = client.send("Hello, Server!", wait_response=True)
print(f"Server response: {response}")

# Disconnect
client.disconnect()
```

## Configuration

### Server Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | "127.0.0.1" | Server host address |
| `port` | int | 8080 | Server port |
| `compression_type` | str | "zlib" | Compression algorithm (zlib, lzma, bz2) |
| `compression_level` | int | 6 | Compression level (1-9) |
| `compress` | bool | True | Enable compression |
| `keepalive_interval` | float | 30.0 | Keepalive interval in seconds |
| `keepalive_max_missed` | int | 3 | Max missed keepalives before disconnect |
| `recv_buffer_size` | int | 65536 | Receive buffer size |
| `send_buffer_size` | int | 65536 | Send buffer size |

### Client Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | "127.0.0.1" | Server host address |
| `port` | int | 8080 | Server port |
| `compression_type` | str | "zlib" | Compression algorithm (zlib, lzma, bz2) |
| `compression_level` | int | 6 | Compression level (1-9) |
| `compress` | bool | True | Enable compression |
| `keepalive_interval` | float | 30.0 | Keepalive interval in seconds |
| `keepalive_max_missed` | int | 3 | Max missed keepalives before disconnect |
| `connection_timeout` | float | 10.0 | Connection timeout in seconds |
| `recv_buffer_size` | int | 65536 | Receive buffer size |
| `send_buffer_size` | int | 65536 | Send buffer size |

## API Reference

### TcpServer

#### Methods

- `start()` - Start the server
- `stop()` - Stop the server and disconnect all clients
- `wait()` - Block until server stops
- `start_and_wait()` - Start server and block
- `send_client(client_addr, data, path=None, wait_response=False)` - Send data to specific client
- `disconnect_client(client_addr)` - Disconnect a specific client
- `get_connected_clients()` - Get number of connected clients
- `is_connected(client_addr)` - Check if client is connected
- `event(event_type)` - Decorator to register event handler
- `path(path, middleware=None)` - Decorator to register path handler
- `register_blueprint(blueprint)` - Register a blueprint

### TcpClient

#### Methods

- `connect()` - Connect to server
- `disconnect()` - Disconnect from server
- `send(data, path=None, wait_response=False)` - Send data to server
- `wait()` - Block until client disconnects
- `connect_and_wait()` - Connect and block
- `is_connected()` - Check if connected
- `event(event_type)` - Decorator to register event handler
- `path(path, middleware=None)` - Decorator to register path handler
- `register_blueprint(blueprint)` - Register a blueprint

## Events

### Server Events

- `EventType.Server.START` - Server started
- `EventType.Server.STOP` - Server stopped
- `EventType.Server.CLIENT_CONNECT` - Client connected
- `EventType.Server.CLIENT_DISCONNECT` - Client disconnected
- `EventType.Server.MESSAGE` - Message received from client

### Client Events

- `EventType.Client.CONNECT` - Connected to server
- `EventType.Client.DISCONNECT` - Disconnected from server
- `EventType.Client.MESSAGE` - Message received from server

### Global Events

- `EventType.Global.ERROR` - Error occurred

## Path-Based Routing

Send messages to specific handlers using paths:

```python
# Server
@server.path("/user/login")
def handle_login(data):
    # Handle login
    pass

@server.path("/user/register")
def handle_register(data):
    # Handle registration
    pass

# Client
client.send(data, path="/user/login")
```

## Blueprints

Organize your code with blueprints:

```python
from socketflow import Blueprint

user_bp = Blueprint("user")

@user_bp.path("/login")
def login(data):
    pass

@user_bp.path("/register")
def register(data):
    pass

# Register blueprint
server.register_blueprint(user_bp)
```

## Keepalive

SocketFlow implements bidirectional keepalive at two levels:

1. **Application-Level Keepalive** - Custom ping/pong messages
2. **TCP-Level Keepalive** - OS-managed keepalive probes

Both client and server independently monitor connection health based on their own configurations.

## Compression

Support for multiple compression algorithms:

- **zlib** - Fast compression, good balance
- **lzma** - High compression ratio, slower
- **bz2** - Good compression, moderate speed

## Error Handling

SocketFlow provides custom exception types:

- `NotConnected` - Connection not established
- `ConnectionTimeout` - Connection attempt timed out
- `KeepaliveTimeout` - Keepalive timeout
- `CompressionError` - Compression/decompression error
- `InvalidData` - Invalid message format
- `NoResponse` - No response received within timeout
- `MessageHandlerError` - Message handling error

## License

MIT License - see LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Support

- GitHub Issues: https://github.com/ayammaximilian/socketflow/issues
- Documentation: https://socketflow.readthedocs.io/

## Requirements

- Python 3.7+
- No external dependencies (uses only standard library)
