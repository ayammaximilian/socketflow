import pickle
from typing import Any, Dict, Tuple

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

    @classmethod
    def available_methods(cls):
        return [m for m, (ok, *_) in cls._REGISTRY.items() if ok]

    # ---------- OBJECT (pickle) ----------

    @classmethod
    def compress(
        cls,
        obj: Any,
        method: str = "zstd",
        level: int | None = None,
        pickle_protocol: int = pickle.HIGHEST_PROTOCOL,
    ) -> bytes:
        data = pickle.dumps(obj, protocol=pickle_protocol)
        return cls.compress_bytes(data, method, level)

    @classmethod
    def decompress(cls, data: bytes) -> Any:
        raw = cls.decompress_bytes(data)
        return pickle.loads(raw)

    # ---------- BYTES (in-memory / file) ----------

    @classmethod
    def compress_bytes(
        cls,
        data: bytes,
        method: str = "zstd",
        level: int | None = None,
    ) -> bytes:
        method = method.lower()
        if method not in cls._REGISTRY:
            raise ValueError(f"Unsupported method '{method}'")

        available, compress_fn, _, default_level = cls._REGISTRY[method]
        if not available:
            raise ValueError(f"Method '{method}' not available")

        if level is None:
            level = default_level

        compressed = compress_fn(data, level)
        return method.encode() + cls.HEADER_SEP + compressed

    @classmethod
    def decompress_bytes(cls, data: bytes) -> bytes:
        try:
            method_raw, payload = data.split(cls.HEADER_SEP, 1)
        except ValueError:
            raise ValueError("Invalid data header")

        method = method_raw.decode()
        if method not in cls._REGISTRY:
            raise ValueError(f"Unknown method '{method}'")

        available, _, decompress_fn, _ = cls._REGISTRY[method]
        if not available or decompress_fn is None:
            raise ValueError(f"Method '{method}' not available")

        return decompress_fn(payload)
