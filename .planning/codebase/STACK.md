# Technology Stack

**Analysis Date:** 2026-02-22

## Languages

**Primary:**
- Python 3.12 - All application code (`remind_me_mcp.py`)

**Secondary:**
- JavaScript/JSX - Dashboard UI artifact (`remind_me_dashboard.jsx`, standalone React file)

## Runtime

**Environment:**
- Python 3.12 (specified in `.python-version`)
- `requires-python = ">=3.11"` (minimum requirement per `pyproject.toml`)

**Package Manager:**
- `uv` recommended (referenced throughout `README.md` for install and run commands)
- `pip` also supported
- Lockfile: Not present (no `uv.lock` or `requirements.txt`)

## Frameworks

**Core:**
- `mcp[cli]` >=1.0.0 - Model Context Protocol server framework (`FastMCP` class)
- `starlette` >=0.40.0 - ASGI web framework for the optional HTTP dashboard/REST API
- `uvicorn` >=0.30.0 - ASGI server that runs the Starlette dashboard app

**Testing:**
- Not detected (no test files, no pytest/unittest config)

**Build/Dev:**
- `hatchling` - Build backend (declared in `pyproject.toml` `[build-system]`)

## Key Dependencies

**Critical:**
- `mcp[cli]` >=1.0.0 - FastMCP server runtime; all tool registration and stdio transport
- `pydantic` >=2.0.0 - Data validation and model definitions (`BaseModel`, `Field`, `field_validator`)
- `starlette` >=0.40.0 - HTTP routing and request handling for the dashboard REST API
- `uvicorn` >=0.30.0 - Serves the Starlette app when `--serve-ui` mode is active
- `numpy` >=1.24.0 - Vector math for embedding mean-pooling and L2 normalization

**Infrastructure (optional semantic search extras):**
- `sqlite-vec` >=0.1.0 - SQLite extension for vector similarity search (cosine)
- `onnxruntime` >=1.16.0 - Local ONNX inference for generating text embeddings
- `tokenizers` >=0.15.0 - HuggingFace tokenizer for the embedding model
- `huggingface-hub` >=0.20.0 - Downloads the `all-MiniLM-L6-v2` ONNX model on first use

## Configuration

**Environment:**
- `REMIND_ME_MCP_DIR` - Data directory path (default: `~/.remind-me`)
- `REMIND_ME_MCP_SERVE_UI` - Set to `true` to start HTTP dashboard instead of stdio MCP
- `REMIND_ME_MCP_UI_PORT` - Dashboard port (default: `5199`)
- `REMIND_ME_EMBEDDING_MODEL` - HuggingFace model ID (default: `sentence-transformers/all-MiniLM-L6-v2`)
- No `.env` file detected; env vars passed via MCP config JSON or shell

**Build:**
- `pyproject.toml` - Package metadata, dependencies, build backend, and entry point script

## Platform Requirements

**Development:**
- Linux/macOS/Windows with Python 3.11+
- `uv` or `pip` for dependency management
- No Docker or external services required for core functionality
- Optional: ~80MB disk for ONNX embedding model cache in `~/.remind-me/models/`

**Production:**
- Runs as an MCP stdio server invoked by Claude Code, Claude Desktop, or Claude.ai
- Can also run as a standalone HTTP server (`--serve-ui` mode) on any host
- Single-file deployment: `remind_me_mcp.py` is the entire server
- Data directory (`~/.remind-me/`) is the only persistent artifact

---

*Stack analysis: 2026-02-22*
*Update after major dependency changes*
