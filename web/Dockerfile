# ARM64 web tier: FastAPI serving the frontend build and signing calls to AgentCore Runtime.
# Build from the repository root, so both `frontend/` and `web/` are in the context:
#   docker build --platform linux/arm64 -f web/Dockerfile -t ielts-web:dev .
#
# Base images come from public.ecr.aws, not Docker Hub: Hub is blocked on the build machine and a
# `FROM node:22-slim` here fails at pull with no useful message.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# `npm run build` is `tsc -b && vite build`, so a type error fails the image build. That is
# intentional: shipping a bundle that does not type-check has no upside.
RUN npm run build

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_STATIC_DIR=/app/web/static \
    PORT=80

WORKDIR /app
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn>=0.30" \
        "boto3>=1.40"

COPY web/ /app/web/
COPY --from=frontend /build/dist/ /app/web/static/

# Non-root, binding port 80. That combination is normally impossible, and it works here for one
# specific reason: container runtimes set `net.ipv4.ip_unprivileged_port_start=0` in the
# container's own network namespace. Measured on this machine (linux/arm64, Docker 29.5):
# uid 10001 binds 0.0.0.0:80 successfully, with CapEff=0 -- so it is the sysctl doing the work,
# not a capability. `setcap cap_net_bind_service` on the interpreter was tried and had no effect
# (the file capability does not survive into CapPrm here), so it is deliberately not used.
#
# The port cannot move: awsvpc networking exposes the container port directly with no remapping,
# and deploy/config.sh pins 80 because many corporate networks block outbound 8080.
#
# If a future platform ships a non-zero ip_unprivileged_port_start, this container fails loudly at
# startup with EACCES rather than misbehaving. The one-line fix is a `systemControls` entry in the
# task definition setting `net.ipv4.ip_unprivileged_port_start=0`.
RUN useradd --create-home --uid 10001 ielts && chown -R ielts:ielts /app
USER ielts

EXPOSE 80
CMD ["python", "-m", "web.app"]
