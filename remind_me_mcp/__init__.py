"""
remind_me_mcp — Persistent, searchable memory across Claude interfaces.

Package layout:
  config      — Module-level constants and environment configuration
  models      — Pydantic input models and ResponseFormat enum
  formatting  — Memory formatting helpers
  db          — Database connection, schema, and helpers
  embeddings  — ONNX embedding engine
"""

from __future__ import annotations
