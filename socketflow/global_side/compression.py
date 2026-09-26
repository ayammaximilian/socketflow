import base64
import json
import pickle
from typing import Any, Dict, Optional, Tuple

try:
    import lzma

    HAS_LZMA = True
except ImportError:
    HAS_LZMA = False

try:
    import bz2

    HAS_BZ2 = True
except ImportError:
    HAS_BZ2 = False

try:
    import zlib

    HAS_ZLIB = True
except ImportError:
    HAS_ZLIB = False

try:
    import gzip

    HAS_GZIP = True
except ImportError:
    HAS_GZIP = False

try:
    import zstandard as zstd

    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

try:
    import brotli

    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False


class MultiCompressor:
    """
    NOTE:
    - pickle methods are unsafe for untrusted data
    - bytes methods are safe
    """

    _REGISTRY: Dict[str, Tuple[bool, callable, callable, int]] = {
        "zstd": (
            HAS_ZSTD,
            lambda d, level: zstd.compress(d, level=level),
            zstd.decompress if HAS_ZSTD else None,
            22,
        ),
        "brotli": (
            HAS_BROTLI,
            lambda d, level: brotli.compress(d, quality=level),
            brotli.decompress if HAS_BROTLI else None,
            11,
        ),
        "lzma": (
            HAS_LZMA,
            lambda d, level: lzma.compress(d, preset=level),
            lzma.decompress if HAS_LZMA else None,
            9,
        ),
        "bz2": (
            HAS_BZ2,
            lambda d, level: bz2.compress(d, compresslevel=level),
            bz2.decompress if HAS_BZ2 else None,
            9,
        ),
        "zlib": (
            HAS_ZLIB,
            lambda d, level: zlib.compress(d, level=level),
            zlib.decompress if HAS_ZLIB else None,
            9,
        ),
        "gzip": (
            HAS_GZIP,
            lambda d, level: gzip.compress(d, compresslevel=level),
            gzip.decompress if HAS_GZIP else None,
            9,
        ),
    }

    HEADER_SEP = b":"
    SAFE_PAYLOAD_MARKER = b"socketflow-json:"
    BYTES_TYPE_MARKER = "__socketflow_bytes__"

    # Optional codecs ship outside the standard library. Map each one to the
    # package that provides it so the error can say what to install.
    _OPTIONAL_PACKAGE = {
        "zstd": "zstandard",
        "brotli": "Brotli",
    }

    @classmethod
    def available_methods(cls):
        return [m for m, (ok, *_) in cls._REGISTRY.items() if ok]

    @classmethod
    def _unavailable_message(cls, method: str) -> str:
        package = cls._OPTIONAL_PACKAGE.get(method)
        available = ", ".join(cls.available_methods()) or "none"
        if package:
            return (
                f"Compression method '{method}' is not available because "
                f"'{package}' is not installed. Install it with "
                f"'pip install socketflow[{method}]', or choose one of: {available}."
            )
        return (
            f"Compression method '{method}' is not available on this system. "
            f"Choose one of: {available}."
        )

    @classmethod
    def _require(cls, method: str) -> None:
        """Raise a helpful error if the requested codec cannot be used."""
        entry = cls._REGISTRY.get(method)
        if entry is None:
            available = ", ".join(cls._REGISTRY) or "none"
            raise ValueError(
                f"Unknown compression method '{method}'. Choose one of: {available}."
            )
        if not entry[0]:
            raise ValueError(cls._unavailable_message(method))

    # ---------- OBJECT (pickle) ----------

    @classmethod
    def _safe_encode(cls, obj: Any) -> Any:
        if isinstance(obj, (bytes, bytearray, memoryview)):
            return {
                cls.BYTES_TYPE_MARKER: base64.b64encode(bytes(obj)).decode("ascii")
            }
        if isinstance(obj, dict):
            return {key: cls._safe_encode(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [cls._safe_encode(value) for value in obj]
        return obj

    @classmethod
    def _safe_decode(cls, obj: Any) -> Any:
        if isinstance(obj, dict) and cls.BYTES_TYPE_MARKER in obj:
            encoded = obj[cls.BYTES_TYPE_MARKER]
            if not isinstance(encoded, str):
                raise ValueError("Invalid bytes payload")
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        if isinstance(obj, dict):
            return {key: cls._safe_decode(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [cls._safe_decode(value) for value in obj]
        return obj

    @classmethod
    def compress(
        cls,
        obj: Any,
        method: str = "zstd",
        level: Optional[int] = None,
        pickle_protocol: int = pickle.HIGHEST_PROTOCOL,
        allow_pickle: bool = False,
    ) -> bytes:
        if allow_pickle:
            data = pickle.dumps(obj, protocol=pickle_protocol)
        else:
            data = cls.SAFE_PAYLOAD_MARKER + json.dumps(
                cls._safe_encode(obj), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        return cls.compress_bytes(data, method, level)

    @classmethod
    def decompress(
        cls,
        data: bytes,
        allow_pickle: bool = False,
        max_output_size: Optional[int] = None,
    ) -> Any:
        raw = cls.decompress_bytes(data, max_output_size=max_output_size)
        if raw.startswith(cls.SAFE_PAYLOAD_MARKER):
            return cls._safe_decode(
                json.loads(raw[len(cls.SAFE_PAYLOAD_MARKER) :].decode("utf-8"))
            )
        if not allow_pickle:
            raise ValueError(
                "Unsafe pickle payload received; enable allow_pickle only for trusted peers"
            )
        return pickle.loads(raw)

    # ---------- BYTES (in-memory / file) ----------

    @classmethod
    def compress_bytes(
        cls,
        data: bytes,
        method: str = "zstd",
        level: Optional[int] = None,
    ) -> bytes:
        method = method.lower()
        cls._require(method)

        available, compress_fn, _, default_level = cls._REGISTRY[method]
        if not available:
            raise ValueError(cls._unavailable_message(method))

        if level is None:
            level = default_level

        compressed = compress_fn(data, level)
        return method.encode() + cls.HEADER_SEP + compressed

    @staticmethod
    def _bounded_decompress(method, payload, max_output_size):
        if max_output_size is None:
            return None

        if method in ("zlib", "gzip") and HAS_ZLIB:
            wbits = zlib.MAX_WBITS if method == "zlib" else 16 + zlib.MAX_WBITS
            decompressor = zlib.decompressobj(wbits)
            output = decompressor.decompress(payload, max_output_size + 1)
            if len(output) > max_output_size or decompressor.unconsumed_tail:
                raise ValueError("Decompressed message exceeds the configured limit")
            remaining = max_output_size + 1 - len(output)
            if remaining > 0:
                output += decompressor.flush(remaining)
            if len(output) > max_output_size or not decompressor.eof:
                raise ValueError("Decompressed message is invalid or too large")
            return output

        if method == "lzma" and HAS_LZMA:
            decompressor = lzma.LZMADecompressor()
            output = decompressor.decompress(payload, max_output_size + 1)
            if len(output) > max_output_size or decompressor.unconsumed_tail:
                raise ValueError("Decompressed message exceeds the configured limit")
            if not decompressor.eof:
                raise ValueError("Decompressed message is incomplete")
            return output

        return None

    @classmethod
    def decompress_bytes(
        cls, data: bytes, max_output_size: Optional[int] = None
    ) -> bytes:
        try:
            method_raw, payload = data.split(cls.HEADER_SEP, 1)
        except ValueError:
            raise ValueError("Invalid data header")

        method = method_raw.decode()
        cls._require(method)

        available, _, decompress_fn, _ = cls._REGISTRY[method]
        if not available or decompress_fn is None:
            raise ValueError(cls._unavailable_message(method))
        if max_output_size is not None and max_output_size < 0:
            raise ValueError("max_output_size cannot be negative")

        bounded = cls._bounded_decompress(method, payload, max_output_size)
        if bounded is not None:
            return bounded

        output = decompress_fn(payload)
        if max_output_size is not None and len(output) > max_output_size:
            raise ValueError("Decompressed message exceeds the configured limit")
        return output

