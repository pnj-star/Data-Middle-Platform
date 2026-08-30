"""Wiki domain package (Postgres + wiki-specific Milvus collection).

Per ROADMAP: the wiki is the main product; the legacy file-pipeline is the
internal document-ingestion engine. This package holds the wiki data model
(models.py), and will grow the session/engine wiring (P1-T1), the Alembic
migration config (P0-T3) and the API layer (Phase 1+).
"""
