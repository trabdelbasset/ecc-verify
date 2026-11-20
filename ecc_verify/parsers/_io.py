"""Shared file helpers."""
import gzip
import io
from pathlib import Path


def open_text(path):
    p = str(path)
    if p.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(p, "rb"), encoding="utf-8", errors="replace")
    return open(p, "r", encoding="utf-8", errors="replace")


def read_text(path) -> str:
    with open_text(Path(path)) as f:
        return f.read()


def norm(name: str) -> str:
    """Normalise an identifier: drop verilog/DEF escapes."""
    return name.replace("\\", "").strip()
