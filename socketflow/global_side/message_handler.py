from .message_manager import message_manager
from .compression import MultiCompressor


class MessageHandler:
    @staticmethod
    def unpack_data(
        encoded_message, allow_pickle=False, max_decompressed_size=None
    ):
        try:
            data = message_manager.decode(encoded_message)
            headers = data[0]
            body = data[1] if len(data) > 1 else None
            if headers.get("compressed") and headers.get("type") == "__user__":
                body = MultiCompressor.decompress(
                    body,
                    allow_pickle=allow_pickle,
                    max_output_size=max_decompressed_size,
                )
            return headers, body
        except Exception:
            return None, None

    @staticmethod
    def create_ping():
        """Create a ping message"""
        ping_headers = {
            "type": "__ping__",
        }
        length_bytes, encoded_ping = message_manager.encode_with_length(ping_headers)
        return length_bytes + encoded_ping

    @staticmethod
    def create_pong():
        """Create a pong message in response to a ping"""
        pong_headers = {
            "type": "__pong__",
        }
        length_bytes, encoded_pong = message_manager.encode_with_length(pong_headers)
        return length_bytes + encoded_pong


message_handler = MessageHandler()
