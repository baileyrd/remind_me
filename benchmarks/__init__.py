"""
benchmarks — Retrieval-quality benchmark harness for Remind Me.

This package measures how well ``remind_me_search`` retrieves the right
memories, using the same RRF + hybrid (FTS5 + sqlite-vec) stack the real MCP
server uses. The primary dataset is LongMemEval; the metrics are Recall@k and
MRR computed at *session* granularity, which is directly comparable to the
"R@5" figures other long-term-memory systems publish.

The harness is intentionally kept out of the distributed wheel (the wheel only
packages ``remind_me_mcp``) — it is a developer/research tool, not a runtime
dependency.

See ``benchmarks/README.md`` for download, run, and interpretation notes.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
