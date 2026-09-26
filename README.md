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

## Non-Blocking Request/Reply

`send_async` returns a request handle immediately instead of blocking the calling thread:

```python
request = client.send_async("Hello, Server!", path="echo", timeout=5.0)
print("Sent, not waiting")

# Check later
if request.done():
    print(request.result())

# Or wait only as long as you want
try:
    response = request.result(timeout=2)
except NoResponse:
    print("Timed out")
```

Handles also support callbacks and cancellation:

```python
def on_reply(handle):
    print("Reply:", handle.result().data)

request = client.send_async("Hello!", path="echo")
request.add_done_callback(on_reply)

# Give up on a request that is no longer needed
request.cancel()
```

Handle methods: `done()`, `result(timeout=None)`, `exception(timeout=None)`, `cancel()`, `cancelled()`, `add_done_callback(fn)`, and the `data_id` attribute.

The server works the same way:

```python
request = server.send_client_async(client_addr, "push", path="push")
response = request.result(timeout=5)
```

Notes:
- `send(..., wait_response=True)` still works and is now built on the same handle.
- Timeouts run on one shared timer thread per client/server, not one thread per request.
- Cancelling or timing out removes the request from tracking immediately.

### Pending request tracking

Every in-flight request is held in `pending_responses` until it completes. The default
`timeout=30.0` is what keeps this bounded: when it expires, the request is removed
automatically even if no reply ever arrives.

```python
request = client.send_async("hello", path="echo", timeout=30.0)
```

Use `timeout=None` only when a reply is genuinely optional. In that case the request is
kept until the connection closes, so `len(client.pending_responses)` grows with every
unanswered request:

```python
request = client.send_async("fire-and-forget", path="notify", timeout=None)
print(len(client.pending_responses))  # grows while replies are missing
```

To keep a hard ceiling, cancel explicitly or check the count before sending.

## Installation

SocketFlow has **no required dependencies**. It runs on the Python standard library
alone, so installing it never pulls anything else in.

```bash
pip install socketflow
```

### Optional compression codecs

Four codecs are built in: `zlib`, `lzma`, `bz2`, and `gzip`. Two more are available
through extras:

```bash
pip install socketflow[zstd]      # adds zstandard
pip install socketflow[brotli]    # adds Brotli
pip install socketflow[all]       # adds both
```

Then use them like any other codec:

```python
server = TcpServer(compression_type="zstd")
```

If a codec is not installed, you get a message telling you exactly what to do:

```
Compression method 'zstd' is not available because 'zstandard' is not
installed. Install it with 'pip install socketflow[zstd]', or choose one of:
lzma, bz2, zlib, gzip.
```

Check what your machine can do:

```python
from socketflow.global_side.compression import MultiCompressor
print(MultiCompressor.available_methods())
```

## Logging and Metrics

Two tools for seeing what your server is doing. **Both are off by default**, so adding
them changes nothing until you ask for them.

### Logging

Instead of reading log lines and guessing, each entry carries labeled fields.

```python
from socketflow import logs

logs.configure(level="INFO")              # human-readable, to stderr
logs.configure(level="INFO", json_output=True)   # one JSON object per line
```

Sample output:

```json
{"time": "2026-09-26T02:00:08.616Z", "level": "INFO", "logger": "socketflow.server",
 "message": "client connected", "client_identity": "test-client", "active_clients": 1}
```

Send logs somewhere else by giving it a sink:

```python
logs.configure(level="DEBUG", sinks=[logs.MemorySink(limit=500)])
logs.configure(level="DEBUG", sinks=[logs.CallbackSink(my_function)])
logs.configure(level="DEBUG", sinks=[])   # silence completely
```

| Level | Shows |
|---|---|
| `DEBUG` | Every message, sent and received |
| `INFO` | Connections, server start/stop, drains |
| `WARNING` | Rejected connections, disconnects |
| `ERROR` | Failures |

Write your own logs the same way:

```python
log = logs.get_logger("my.app").bind(service="billing")
log.info("charge accepted", order_id=123)   # every line now has service=billing
```

#### Long messages

`message` is always written out in full. A 50,000-character message is logged as
50,000 characters â€” it is not silently shortened, and JSON output stays valid.

To stop one huge value from flooding a sink, set a limit:

```python
logs.configure(level="INFO", json_output=True, max_message_length=2000)
```

Anything longer is cut and marked:

```
"message": "A very long value ... [truncated 48213 chars]"
```

The limit applies to `message` and to any string field. It is off by default
(`None`), so nothing changes unless you ask for it.

#### Payloads are never logged

SocketFlow logs metadata about messages, never their content. Sending a 2 MB message
produces `{"message": "message received", "path": "echo"}` â€” the data is not written
anywhere.

The thing to watch is your own code. `log.info(f"got {payload}")` will happily log a
2 MB payload. Log identifiers and sizes, not bodies.

### Metrics

Counters, gauges, and timing histograms. The server records them automatically.

```python
print(server.metrics.counter_value("messages_received_total", path="echo"))
print(server.metrics.gauge_value("connections_active"))
print(server.metrics_snapshot())     # nested dict, JSON-friendly
print(server.metrics_text())        # Prometheus format
```

| Metric | Type | Meaning |
|---|---|---|
| `connections_accepted_total` | counter | Clients that connected |
| `connections_rejected_total` | counter | Clients turned away |
| `connections_active` | gauge | Connected right now |
| `messages_received_total` | counter | By path |
| `messages_sent_total` | counter | To clients |
| `bytes_sent_total` | counter | Bytes written |
| `errors_total` | counter | By context (`server.handle_data`, `client.receive`, â€¦) |
| `backpressure_total` | counter | Rejections from full queues |

### Feeding Prometheus

`metrics_text()` is already Prometheus format, so expose it over HTTP:

```python
# In test_server.py
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = server.metrics_text().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)
```

Then scrape `http://your-host:9090/metrics`:

```
socketflow_connections_accepted_total 1
socketflow_connections_active 1
socketflow_messages_received_total{path="echo"} 4
```

### The catch

Labels create one time series per value. If you label with something unique â€” a
`data_id`, a full URL with an ID, a raw socket address â€” your metrics will grow
endlessly and can slow the server down. Label with small, bounded values like
`path` or `reason`.

The library labels by `path` and `reason` only, so this is safe unless you add your own.

## Mutual TLS (Client Certificates)

Like a door that checks **both** badges. The server proves who it is, and the client
proves who it is. After the handshake, the server knows the client's name.

```python
server = TcpServer(
    tls_enabled=True,
    tls_certfile="server.crt",
    tls_keyfile="server.key",
    tls_client_ca="clients.crt",           # CA that signs client certs
    tls_require_client_cert=True,          # no cert, no entry
)

client = TcpClient(
    tls_enabled=True,
    tls_ca_certs="ca.crt",
    tls_server_hostname="server.example.com",
    tls_certfile="me.crt",                 # my badge
    tls_keyfile="me.key",
)
```

Handlers see who is talking:

```python
@server.path("whoami")
def whoami(message):
    print(message.client_identity)   # "laptop-07"
    server.send_client(message.client_addr, message.client_identity, message.data_id)

@server.event(EventType.Server.CLIENT_CONNECT)
def on_connect(data):
    print(f"{data.client_identity} joined")   # "laptop-07"
```

Or outside a handler:

```python
server.get_client_identity(client_addr)   # "laptop-07" or None
```

### Required vs optional

| Setting | No client cert | Bad client cert |
|---|---|---|
| `tls_require_client_cert=True` | Rejected ðŸ”’ | Rejected ðŸ”’ |
| `tls_client_ca` only (optional) | Allowed, `client_identity` is `None` | Rejected ðŸ”’ |
| No `tls_client_ca` | Normal one-way TLS | Normal one-way TLS |

### The catch

Certificates expire. If one does, the client is locked out until you issue a new one.
Plan renewal before the expiry date, and keep the CA private key safe â€” anyone holding
it can mint a certificate your server will trust.

## Protocol Version Negotiation

Like a phone that only works on certain networks. Both sides say which versions they
speak, then agree on one.

It is **off by default**, so existing code is unaffected. Turn it on by passing
`protocol_version` or a version range:

```python
server = TcpServer(protocol_version=1)          # speaks exactly version 1
client = TcpClient(protocol_version=1)          # speaks exactly version 1
client.connect()
print(client.negotiated_protocol_version)        # 1
```

Ranges let old and new builds talk to each other:

```python
server = TcpServer(min_protocol_version=1, max_protocol_version=3)
client = TcpClient(min_protocol_version=1, max_protocol_version=5)

client.connect()
print(client.negotiated_protocol_version)        # 3, the highest both support
```

If there is no overlap, the connection is refused with a clear reason:

```python
client = TcpClient(min_protocol_version=7, max_protocol_version=9)

try:
    client.connect()
except ProtocolVersionError as error:
    print(error)
    # No common protocol version: client supports 7-9, server supports 1-1
```

The check runs during the handshake, **before authentication**, so a mismatched peer
never reaches your credentials.

| | Old | New |
|---|---|---|
| Mismatched versions | Weird errors, or silent breakage | Clear `ProtocolVersionError` |
| Upgrading server | May break old clients | Old clients keep working |
| Knowing what runs | Guesswork | `negotiated_protocol_version` |

Notes:
- Both sides must opt in. If only one side negotiates, no version is recorded and the
  handshake proceeds as before.
- The server stores the agreed version per connection; the client exposes it as
  `negotiated_protocol_version`.

## Graceful Shutdown

Draining lets in-flight work finish before connections are closed, so replies that are
already being produced are not lost.

```python
# Stop accepting new clients, wait for handlers and queued replies, then close.
finished = server.drain(timeout=10.0)
print("drained cleanly:", finished)
```

While draining:

- New connections are refused immediately.
- Existing clients stay connected.
- Running and queued handlers are allowed to finish.
- Replies those handlers queued are flushed to the socket.

`drain()` returns `True` if everything finished before the timeout, `False` if the
timeout expired first. It does not close connections by itself.

```python
# Combined stop, with draining
server.stop(drain=True, drain_timeout=10.0)

# shutdown() drains by default
server.shutdown()                       # drains, then stops
server.shutdown(drain=False)            # immediate, no drain
```

To observe a drain starting:

```python
@server.event(EventType.Server.DRAINING)
def on_draining(data):
    print(f"draining: {data.connected_clients} clients still connected")
```

`server.draining` is `True` while a drain is in progress.

Notes:
- Always pass a `drain_timeout`. A handler that blocks forever will make draining wait
  until the timeout, then return `False`.
- Draining waits for handler tasks, not for clients to disconnect. Long-lived clients
  that send nothing will still hold connections open until `stop()` closes them.

## Connection Protection

Use TLS together with token or username/password authentication:

```python
server = TcpServer(
    host="0.0.0.0",
    port=8080,
    tls_enabled=True,
    tls_certfile="server.crt",
    tls_keyfile="server.key",
    auth_token="replace-with-a-secret",
)

client = TcpClient(
    host="server.example.com",
    port=8080,
    tls_enabled=True,
    tls_ca_certs="ca.crt",
    tls_server_hostname="server.example.com",
    auth_token="replace-with-a-secret",
)
```

The client must trust the server certificate and use the matching server name. Authentication happens before normal application messages are accepted. The client also waits for the server's final `handshake_ok` confirmation. Username/password authentication is also available through `auth_username` and `auth_password`.

## Configuration

### Server Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | "127.0.0.1" | Server host address |
| `port` | int | 8080 | Server port |
| `compression_type` | str | "zlib" | Codec: zlib, lzma, bz2, gzip (built in), or zstd, brotli (optional) |
| `compression_level` | int | 6 | Compression level (1-9) |
| `compress` | bool | True | Enable compression |
| `keepalive_interval` | float | 30.0 | Keepalive interval in seconds |
| `keepalive_max_missed` | int | 3 | Max missed keepalives before disconnect |
| `recv_buffer_size` | int | 65536 | Receive buffer size |
| `send_buffer_size` | int | 65536 | Send buffer size |
| `max_frame_size` | int | 8388608 | Maximum inbound/outbound frame size |
| `max_outbound_queue_bytes` | int | 16777216 | Per-client queued outbound bytes |
| `max_pending_writes` | int | 1000 | Maximum queued outbound frames per client |
| `max_dispatch_workers` | int | 32 | Maximum application handler workers |
| `max_pending_tasks` | int | 1000 | Maximum queued/running handler tasks |
| `dispatch_queue_timeout` | float | 1.0 | Seconds to wait when dispatch capacity is exhausted |
| `max_connections` | int | 1000 | Maximum concurrently accepted clients |
| `allow_pickle` | bool | False | Explicitly allow legacy pickle payloads from trusted peers |
| `tls_enabled` | bool | False | Encrypt the connection with TLS |
| `tls_certfile` | str | None | Server TLS certificate file |
| `tls_keyfile` | str | None | Server TLS private key file |
| `tls_client_ca` | str | None | CA used to verify client certificates (mutual TLS) |
| `tls_require_client_cert` | bool | False | Reject clients that do not present a certificate |
| `tls_handshake_timeout` | float | 10.0 | TLS handshake timeout |
| `auth_enabled` | bool | False | Require the authentication handshake |
| `auth_token` | str | None | Shared authentication token |
| `auth_username` | str | None | Username for authentication |
| `auth_password` | str | None | Password for authentication |
| `auth_timeout` | float | 30.0 | Authentication timeout |
| `require_handshake` | bool | True | Require the security handshake before messages |
| `handshake_timeout` | float | None | Full TLS/server-ready/auth handshake timeout; defaults to the connection timeout |
| `max_memory_bytes` | int | 268435456 | Shared memory budget for connection buffers and queued writes |
| `max_decompressed_size` | int | 16777216 | Maximum size after decompression |
| `use_event_loop` | bool | True | Use the shared selector loop for server connections |

### Client Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | "127.0.0.1" | Server host address |
| `port` | int | 8080 | Server port |
| `compression_type` | str | "zlib" | Codec: zlib, lzma, bz2, gzip (built in), or zstd, brotli (optional) |
| `compression_level` | int | 6 | Compression level (1-9) |
| `compress` | bool | True | Enable compression |
| `keepalive_interval` | float | 30.0 | Keepalive interval in seconds |
| `keepalive_max_missed` | int | 3 | Max missed keepalives before disconnect |
| `connection_timeout` | float | 10.0 | Connection timeout in seconds |
| `recv_buffer_size` | int | 65536 | Receive buffer size |
| `send_buffer_size` | int | 65536 | Send buffer size |
| `max_frame_size` | int | 8388608 | Maximum inbound/outbound frame size |
| `max_outbound_queue_bytes` | int | 16777216 | Maximum queued outbound bytes |
| `max_pending_writes` | int | 1000 | Maximum queued outbound frames |
| `max_dispatch_workers` | int | 32 | Maximum application handler workers |
| `max_pending_tasks` | int | 1000 | Maximum queued/running handler tasks |
| `dispatch_queue_timeout` | float | 1.0 | Seconds to wait when dispatch capacity is exhausted |
| `allow_pickle` | bool | False | Explicitly allow legacy pickle payloads from trusted peers |
| `tls_enabled` | bool | False | Encrypt the connection with TLS |
| `tls_ca_certs` | str | None | Trusted server CA certificate file |
| `tls_server_hostname` | str | None | Name used to verify the server certificate |
| `tls_certfile` | str | None | Client certificate presented for mutual TLS |
| `tls_keyfile` | str | None | Private key for the client certificate |
| `auth_enabled` | bool | False | Send the authentication handshake |
| `auth_token` | str | None | Shared authentication token |
| `auth_username` | str | None | Username for authentication |
| `auth_password` | str | None | Password for authentication |
| `auth_timeout` | float | 30.0 | Authentication timeout |
| `require_handshake` | bool | True | Require the security handshake before messages |
| `handshake_timeout` | float | None | Full TLS/server-ready/auth handshake timeout; defaults to the authentication timeout |
| `max_memory_bytes` | int | 67108864 | Shared memory budget for this client's buffers and queued writes |
| `max_decompressed_size` | int | 16777216 | Maximum size after decompression |

## Transport Reliability

- Inbound frames are length-prefixed and validated against `max_frame_size`.
- Each connection has one serialized outbound writer, preventing frame interleaving.
- Outbound queues are bounded by both bytes and frame count; overload raises `Backpressure`.
- Application handlers use a bounded worker pool instead of one thread per message.
- Pending responses are isolated per client connection on the server.
- `max_memory_bytes` limits buffered receive data and queued outgoing data across all connections.
- `max_decompressed_size` rejects compressed messages that expand beyond the allowed size.
- Server connections use one shared `selectors` event loop by default; application handlers still use the bounded worker pool.
- `TCP_NODELAY` is enabled for low-latency request/response traffic.
- Use `shutdown()` when permanently closing a client or server; use `stop()`/`disconnect()` when a restartable lifecycle is needed.
- Compressed application payloads use safe JSON/bytes serialization by default; `allow_pickle=True` is only for explicitly trusted legacy peers.

## API Reference

### TcpServer

#### Methods

- `start()` - Start the server
- `stop(drain=False, drain_timeout=10.0)` - Stop the server and disconnect all clients
- `drain(timeout=10.0, reason="shutdown")` - Finish in-flight work, then report success
- `shutdown(drain=True, drain_timeout=10.0)` - Permanently stop the server and dispatcher
- `draining` - True while a drain is in progress
- `wait()` - Block until server stops
- `start_and_wait()` - Start server and block
- `send_client(client_addr, data, path=None, wait_response=False)` - Send data to specific client
- `send_client_async(client_addr, data, path=None, timeout=30.0)` - Send without blocking, returns a request handle
- `disconnect_client(client_addr)` - Disconnect a specific client
- `get_connected_clients()` - Get number of connected clients
- `get_client_identity(client_addr)` - Certificate common name for a client, or None
- `metrics` - The `MetricsRegistry` recording this server's metrics
- `metrics_snapshot()` - All metrics as a nested dict
- `metrics_text()` - All metrics in Prometheus text format
- `is_connected(client_addr)` - Check if client is connected
- `event(event_type)` - Decorator to register event handler
- `path(path, middleware=None)` - Decorator to register path handler
- `register_blueprint(blueprint)` - Register a blueprint

### TcpClient

#### Methods

- `connect()` - Connect to server
- `disconnect()` - Disconnect from server
- `shutdown()` - Permanently disconnect and stop the dispatcher
- `send(data, path=None, wait_response=False)` - Send data to server
- `send_async(data, path=None, timeout=30.0)` - Send without blocking, returns a request handle
- `metrics` - The `MetricsRegistry` recording this client's metrics
- `wait()` - Block until client disconnects
- `connect_and_wait()` - Connect and block
- `is_connected()` - Check if connected
- `event(event_type)` - Decorator to register event handler
- `path(path, middleware=None)` - Decorator to register path handler
- `register_blueprint(blueprint)` - Register a blueprint

## Events

### Server Events

- `EventType.Server.START` - Server started
- `EventType.Server.DRAINING` - Server started draining in-flight work
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
- `Backpressure` - Outbound or dispatch capacity was exhausted
- `DispatcherError` - Dispatcher queue or lifecycle failure
- `AuthenticationError` - Connection credentials were missing or invalid
- `TlsError` - TLS setup or certificate verification failed
- `HandshakeError` - The security handshake did not complete

## License

MIT License - see LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Support

- GitHub Issues: https://github.com/ayammaximilian/socketflow/issues
- Documentation: https://socketflow.dev/

## Requirements

- Python 3.7+
- No external dependencies (uses only standard library)
