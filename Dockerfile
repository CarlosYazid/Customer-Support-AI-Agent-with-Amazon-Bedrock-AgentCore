# ── Etapa 1: Builder (Se ejecuta en la arquitectura del Host) ──
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

COPY pyproject.toml uv.lock* ./

# Forzar a 'uv' a descargar binarios específicos para ARM64 (aarch64)
RUN uv sync --frozen --no-cache --no-install-project --python-platform aarch64-unknown-linux-gnu

# ── Etapa 2: Runtime (ARM64 para AWS AgentCore) ──
FROM python:3.13-slim

WORKDIR /app

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

COPY --from=builder /app/.venv /app/.venv
COPY ./src/customer_support_agent/main.py ./main.py

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]