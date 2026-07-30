"""The web tier: serves the frontend build, owns login, and signs calls to AgentCore.

- ``auth.py``           self-hosted accounts. No AWS, no Cognito, no server-side session table.
- ``runtime_client.py`` the one boto3 call (``invoke_agent_runtime``) plus SigV4, which comes
                        free from the ECS task role's credentials.
- ``fanout.py``         one Runtime invocation per material, merged into one SSE stream.
- ``batch_history.py``  what a batch record is, when it is written, and how its status is derived.
- ``batch_store.py``    that record's storage, in the materials bucket next to ``_candidates/``.
- ``app.py``            FastAPI: the ``/api/*`` gate, the SSE relay, the history routes, the SPA.

The last two are here rather than in ``backend/`` for a structural reason, not a convenient one: the
Runtime is invoked once per material and never sees the batch, so the web tier is the only component
that knows a batch exists as a unit. Recording one anywhere else would mean inventing the grouping a
second time.

Why the web tier and not an nginx in front of it: AgentCore Runtime only accepts SigV4-signed
requests, and a reverse proxy cannot sign. So the tier that holds the credentials must be the
tier that talks to the Runtime -- see deploy-plan.md "Web 层形态 (2026-07-28 修正)".
"""
