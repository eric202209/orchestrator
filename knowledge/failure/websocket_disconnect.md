---
type: debug_case
title: WebSocket Client Disconnect
applies_to: [failure]
tags: [websocket, disconnect, client, streaming]
priority: 7
---

The WebSocket connection to the client was closed before streaming completed. The orchestrator may still finish, but the client will not receive further log events for this session.

Error patterns that match this failure:
- "WebSocket disconnect"
- "websocket.disconnect"
- "WebSocketDisconnect"
- "client disconnected"
- "connection closed by client"
- "starlette.websockets.WebSocketDisconnect"

Root cause: The client (browser, mobile app, or CLI) closed the WebSocket connection. This can happen due to network interruption, browser tab close, app backgrounding, or idle timeout.

Impact: The session continues running in the worker. The client can reconnect and poll for session status via REST. No task retry is needed for a WebSocket disconnect alone.

Recommended action: none — do not retry the task. The underlying task is unaffected. Advise the client to reconnect and check session status.
