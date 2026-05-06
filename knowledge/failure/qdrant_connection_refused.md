---
type: failure_memory
title: Qdrant Connection Refused
applies_to: [failure]
tags: [qdrant, vector, connection, refused]
priority: 10
---

Qdrant vector database is not reachable. Connection was refused on the configured QDRANT_URL.

Error patterns that match this failure:
- "Connection refused"
- "ConnectError: Connection refused"
- "httpx.ConnectError"
- "Failed to connect to Qdrant"
- "qdrant_client.http.exceptions.UnexpectedResponse"

Root cause: Qdrant container is not running, wrong port, or misconfigured QDRANT_URL. Default URL is http://localhost:6333.

Recommended action: stop_retry. Retrying will fail identically until Qdrant is restored. KnowledgeService automatically falls back to SQLite retrieval when Qdrant is unavailable, so knowledge retrieval degrades gracefully. Task execution itself should not fail due to Qdrant being down unless it directly requires vector search.
