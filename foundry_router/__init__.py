"""Foundry Router — self-hosted agentic LLM routing middleware.

Ollama-compatible facade in front of a routing brain that decides, per
request, whether work stays local or escalates to Claude (via Meridian),
with dynamic tool discovery, a self-maintaining model registry, and
usage-aware guardrails. Internal/private-network use only.
"""

# Single source of truth for the app version. pyproject.toml reads this
# dynamically (tool.setuptools.dynamic), and main.py / ui.routes /
# facade.ollama_api all import __version__ rather than repeating a literal,
# so a bump is one edit here.
__version__ = "0.24.0"
