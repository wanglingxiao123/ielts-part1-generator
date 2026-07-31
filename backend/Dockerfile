# ARM64 image for AgentCore Runtime (design.md §1, prd.md R7).
# Build from the repository root so the skill assets are inside the build context:
#   docker build --platform linux/arm64 -f backend/Dockerfile -t ielts-backend:dev .
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    IELTS_SKILLS_ROOT=/app/backend/skills \
    IELTS_SCENARIOS_PATH=/app/config/scenarios.yaml

WORKDIR /app

COPY backend/pyproject.toml /app/backend/pyproject.toml
RUN pip install --no-cache-dir \
        "strands-agents[openai]>=1.50.0" \
        "strands-agents-tools>=0.8.0" \
        "openai>=2.0.0" \
        "bedrock-agentcore>=0.1.0" \
        "pyyaml>=6.0"

COPY backend/ /app/backend/
COPY config/ /app/config/
# audio_storage is imported at run time by the selection path (new_material_id, the candidate
# store, synthesis). Omitting it did not break the build or the health check -- every import of it
# sits inside a function, and the one in `_register` is caught and downgraded to a warning. The
# visible symptom was a material_completed event carrying `material_id: null`, and a cold instance
# 500ing on `list_candidates`; nothing said "missing module" until the traceback was read.
COPY audio_storage/ /app/audio_storage/
# The skill assets are copied at build time and read at runtime. They are never transcribed
# into the backend: the human-maintained files stay the single source of truth.
# Both pools, since IELTS_SKILLS_ROOT points at their parent: an agent whose pool is missing
# activates no skill at all, and the failure surfaces as an unusable model reply.
COPY skills/ /app/backend/skills/

# Non-root. The process only reads skill files and writes to a temp dir.
RUN useradd --create-home --uid 10001 ielts && chown -R ielts:ielts /app
USER ielts

EXPOSE 8080
CMD ["python", "-m", "backend.app"]
