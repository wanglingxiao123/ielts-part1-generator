# ARM64 image for the throwaway timing probe (task 08-06-stage0, design.md D2).
#
#   docker build --platform linux/arm64 -f backend/probe.Dockerfile -t ielts-probe:probe-timing .
#
# Deliberately NOT `backend/Dockerfile`. That image installs strands-agents, openai and pyyaml and
# copies skills/, config/ and audio_storage/ -- none of which the probe uses. Two reasons the
# duplication is worth it:
#
# 1. The probe runs twice and may need its sleep length changed between runs. Rebuilding the full
#    77MB image with the model SDKs to alter a number is wasted minutes.
# 2. More importantly, "the probe cannot call a model" has to be checkable rather than promised. With
#    the SDKs and the skill pool absent from the image, that claim is visible in the COPY list below;
#    with them present it could only be argued from reading the source.
#
# `backend/tests/test_probe_app.py` asserts this file copies none of those directories, so the
# property survives someone later "unifying" the two Dockerfiles.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The only dependency. It provides BedrockAgentCoreApp, which is what decides JSON vs SSE -- and
# that decision is half of what this probe measures, so it has to be the real SDK.
RUN pip install --no-cache-dir "bedrock-agentcore>=0.1.0"

# Two files, both by name rather than `COPY backend/`: a directory copy would quietly pull in
# agents.py, orchestration/ and the rest the moment someone ran this from a full checkout, which is
# exactly what the file-level list is here to prevent. `__init__.py` is docstring-only and is needed
# for `python -m backend.probe_app` to resolve.
COPY backend/__init__.py /app/backend/__init__.py
COPY backend/probe_app.py /app/backend/probe_app.py

RUN useradd --create-home --uid 10001 ielts && chown -R ielts:ielts /app
USER ielts

EXPOSE 8080
CMD ["python", "-m", "backend.probe_app"]
