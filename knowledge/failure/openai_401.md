---
type: failure_memory
title: OpenAI 401 / Missing Embedding Key
applies_to: [failure]
tags: [openai, embedding, auth, 401]
priority: 10
---

OpenAI returned a 401 AuthenticationError during embedding. This means OPENAI_API_KEY is missing, empty, or invalid.

Error patterns that match this failure:
- "AuthenticationError: 401 incorrect api key"
- "AuthenticationError: No API key provided"
- "openai.AuthenticationError: 401"
- "invalid_api_key"

Root cause: The OPENAI_API_KEY environment variable is not set or contains a placeholder value like "no-key".

Recommended action: stop_retry. Retrying will not fix an auth failure. The operator must set a valid OPENAI_API_KEY before restarting the session.

Note: KnowledgeService falls back to SQLite retrieval when embedding fails, so retrieval may still work without a key.
