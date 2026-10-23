"""Project memory (Team Lead piece 4): docs/memory in the game repo, indexed by
the coordinator, sliced per session, written through MEMORY_NOTE report lines
and the chat's typed actions. See manager.py for the API."""

from .layout import FIXED_FILES, MEMORY_DIR, file_ok  # noqa: F401
from .manager import MemoryManager  # noqa: F401
