import struct
from typing import Any
import json


class MessageManager:
    def __init__(self):
        pass

    def encode(self, *messages: Any) -> bytes:
        if not messages:
            raise ValueError("At least one message must be provided")

        encoded_parts = []

        for data in messages:
            if isinstance(data, bytes):
                encoded_parts.append(struct.pack("!B", 0))
                encoded_parts.append(struct.pack("!I", len(data)))
                encoded_parts.append(data)
            elif isinstance(data, str):
                msg_bytes = data.encode("utf-8")
                encoded_parts.append(struct.pack("!B", 1))
                encoded_parts.append(struct.pack("!I", len(msg_bytes)))
                encoded_parts.append(msg_bytes)
            else:
                try:
                    json_str = json.dumps(data, ensure_ascii=False)
                    json_bytes = json_str.encode("utf-8")
                    encoded_parts.append(struct.pack("!B", 2))
                    encoded_parts.append(struct.pack("!I", len(json_bytes)))
                    encoded_parts.append(json_bytes)
                except (TypeError, ValueError) as e:
                    raise TypeError(f"Message is not JSON-serializable: {e}")

        return b"".join(encoded_parts)

    def encode_with_length(self, *messages: Any) -> tuple[bytes, bytes]:
        encoded_payload = self.encode(*messages)
        length_bytes = struct.pack(">I", len(encoded_payload))
        return length_bytes, encoded_payload

    def decode(self, payload: bytes, keys_required: int = None):
        if not payload:
            raise ValueError("Payload cannot be empty")

        messages = []
        offset = 0
        payload_len = len(payload)

        while offset < payload_len:
            if offset + 5 > payload_len:
                raise ValueError("Malformed payload: incomplete header")

            msg_type = struct.unpack("!B", payload[offset : offset + 1])[0]
            offset += 1
            msg_len = struct.unpack("!I", payload[offset : offset + 4])[0]
            offset += 4

            if offset + msg_len > payload_len:
                raise ValueError("Malformed payload: incomplete message data")

            msg_data = payload[offset : offset + msg_len]
            offset += msg_len

            if msg_type == 0:
                messages.append(msg_data)
            elif msg_type == 1:
                try:
                    messages.append(msg_data.decode("utf-8"))
                except UnicodeDecodeError as e:
                    raise ValueError(f"Failed to decode string data: {e}")
            elif msg_type == 2:
                try:
                    json_str = msg_data.decode("utf-8")
                    messages.append(json.loads(json_str))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise ValueError(f"Failed to decode JSON data: {e}")
            else:
                raise ValueError(f"Unknown message type: {msg_type}")

        if keys_required is None:
            return messages
        elif keys_required == 1:
            if len(messages) != 1:
                raise ValueError(f"Expected exactly 1 message, but got {len(messages)}")
            return messages[0]
        elif keys_required > 1:
            if len(messages) != keys_required:
                raise ValueError(
                    f"Expected exactly {keys_required} messages, but got {len(messages)}"
                )
            return tuple(messages)
        else:
            raise ValueError("keys_required must be None, 1, or greater")


message_manager = MessageManager()
