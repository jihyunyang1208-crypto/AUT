# core/symbol_cache.py
from __future__ import annotations
from typing import Optional, Dict
import threading

class _SymbolNameCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_code: Dict[str, str] = {}

    def set(self, code: str, name: str) -> None:
        code6 = str(code)[-6:].zfill(6)
        with self._lock:
            if name and name != "-":
                self._by_code[code6] = name

    def get(self, code: str) -> Optional[str]:
        code6 = str(code)[-6:].zfill(6)
        with self._lock:
            return self._by_code.get(code6)

    def all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._by_code)

symbol_name_cache = _SymbolNameCache()
