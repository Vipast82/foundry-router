"""Foundry Router — self-hosted agentic LLM routing middleware.

Ollama-compatible facade in front of a routing brain that decides, per
request, whether work stays local or escalates to Claude (via Meridian),
with dynamic tool discovery, a self-maintaining model registry, and
usage-aware guardrails. Internal/private-network use only.
"""

__version__ = "0.1.0"
