"""
Tests for remind_me_mcp.storage_interfaces — the storage-layer Protocol
documentation (cognee gap #1, Phase 8, prep-work only).

The primary verification that db.py's real functions satisfy these
Protocols is static: `mypy remind_me_mcp` type-checks the `if TYPE_CHECKING`
block at the bottom of storage_interfaces.py, and a signature mismatch there
is a mypy error, not a test failure. These tests add a runtime-checkable
parallel (parameter name/count comparison via `inspect.signature`) so a
signature drift is caught here too, independent of running mypy, plus basic
sanity that every Protocol is actually a Protocol and is exported.
"""

from __future__ import annotations

import inspect

import remind_me_mcp.db as db
import remind_me_mcp.storage_interfaces as si

# Maps each Protocol to the real db.py function it documents.
_PROTOCOL_TO_IMPL = {
    si.EntityUpserter: db._upsert_entity,
    si.MemoryEntityLinker: db._link_memory_entity,
    si.EntityRelationUpserter: db._upsert_entity_relation,
    si.EntityResolver: db._resolve_entity,
    si.EntityProfileReader: db._entity_profile,
    si.VectorSearcher: db._semantic_search,
    si.ChunkEmbedder: db._embed_and_store,
    si.ChunkBatchEmbedder: db._embed_and_store_rows,
    si.OrphanChunkPruner: db._prune_orphan_chunks,
}


def test_every_protocol_is_exported() -> None:
    for protocol in _PROTOCOL_TO_IMPL:
        assert protocol.__name__ in si.__all__


def test_every_protocol_is_a_protocol_class() -> None:
    import typing

    for protocol in _PROTOCOL_TO_IMPL:
        assert typing.Protocol in protocol.__bases__ or any(
            typing.Protocol in base.__mro__ for base in protocol.__mro__
        )


def test_protocol_call_signatures_match_the_real_functions() -> None:
    """Runtime-checkable parallel to the mypy `if TYPE_CHECKING` verification
    at the bottom of storage_interfaces.py: parameter names must line up
    between each Protocol's __call__ and the db.py function it documents."""
    for protocol, impl in _PROTOCOL_TO_IMPL.items():
        protocol_params = list(inspect.signature(protocol.__call__).parameters)
        # Drop 'self' from the Protocol's __call__ — the real function has no
        # bound receiver, it's a plain module-level function.
        assert protocol_params[0] == "self"
        protocol_params = protocol_params[1:]

        impl_params = list(inspect.signature(impl).parameters)

        assert protocol_params == impl_params, (
            f"{protocol.__name__}.__call__ params {protocol_params} != "
            f"{impl.__name__} params {impl_params}"
        )


def test_all_documented_functions_exist_on_db_module() -> None:
    """Every function storage_interfaces.py documents is still a real,
    public-ish (underscore-prefixed but exported) member of db.py."""
    for impl in _PROTOCOL_TO_IMPL.values():
        assert impl.__name__ in db.__all__
