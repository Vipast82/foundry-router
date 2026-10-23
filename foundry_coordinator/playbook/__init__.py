"""Cross-project playbook (A1): the shared knowledge base beside per-project
memory. See layout.py for the repo shape and manager.py for the API."""

from .layout import NOTE_KEYWORD, PLAYBOOK_ID  # noqa: F401
from .manager import PlaybookManager  # noqa: F401
