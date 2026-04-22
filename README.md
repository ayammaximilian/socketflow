# SocketFlow

High-performance asynchronous networking for Python.

## Features

* **Event-Driven:** Built on top of `asyncio` for maximum concurrency.
* **Ease of Use:** Simplified API for complex socket operations.
* **High Throughput:** Optimized for low-latency data transmission.
* **Flexible:** Support for TCP, UDP, and custom protocols.

## Installation

```bash
pip install socketflow
```

## Quick Start

### Server
```python
import asyncio
from socketflow import FlowServer

async def handle_client(reader, writer):
    data = await reader.read(100)
    writer.write(data)
    await writer.drain()

async def main():
    server = FlowServer('127.0.0.1', 8888)
    await server.start(handle_client)

asyncio.run(main())
```

### Client
```python
import asyncio
from socketflow import FlowClient

async def main():
    client = FlowClient()
    await client.connect('127.0.0.1', 8888)
    await client.send(b'Hello Flow')
    response = await client.receive()
    print(response)

asyncio.run(main())
```

## License

MIT
