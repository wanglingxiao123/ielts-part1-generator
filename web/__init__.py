"""The web tier: serves the frontend build, owns login, and signs calls to AgentCore.

Three files, three jobs:

- ``auth.py``          self-hosted accounts. No AWS, no Cognito, no server-side session table.
- ``runtime_client.py`` the one boto3 call (``invoke_agent_runtime``) plus SigV4, which comes
                       free from the ECS task role's credentials.
- ``app.py``           FastAPI: the ``/api/*`` gate, the SSE relay, and the static SPA.

Why the web tier and not an nginx in front of it: AgentCore Runtime only accepts SigV4-signed
requests, and a reverse proxy cannot sign. So the tier that holds the credentials must be the
tier that talks to the Runtime -- see deploy-plan.md "Web 层形态 (2026-07-28 修正)".
"""
