---
title: Qdrant Startup Failure Falls Back To SQLite
type: debug_case
applies_to:
  - planning
  - failure
tags:
  - knowledge
  - qdrant
  - fallback
failure_signature: qdrant_unavailable
priority: 82
---

Knowledge retrieval should remain useful when Qdrant is unavailable at service startup. Treat Qdrant initialization errors as a degraded index state and fall back to active SQLite knowledge items.

The fallback should return relevant `failure_memory` and `debug_case` items for planning, validation, and failure phases without requiring embeddings or a live vector collection.

This improves recovery rate when infrastructure services are absent or restarted independently.
