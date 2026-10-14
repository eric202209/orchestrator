"""Planning services package.

Import concrete services from their submodules, for example
``app.services.planning.plan_commit_service``.

The package intentionally exposes no package-level exports and performs no
imports. Phase 37 SC2-C1 removed the former lazy ``__getattr__`` export map:
no in-repository consumer used it, and its dormant loader could not be proven
unreachable by the finite Worker root verifier.
"""
