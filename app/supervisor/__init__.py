"""Phase 39 F2 local trusted Supervisor (standard library only).

The Supervisor is an independent process. It must not import the Orchestrator
application, database, Celery, or providers; the Worker reaches it only over
its authenticated Unix domain socket.
"""
