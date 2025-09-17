# ===== Base runtime =====
FROM python:3.12-slim AS runtime

# System setup
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system deps (curl for HEALTHCHECK)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install uv (fast, deterministic Python deps)
RUN python -m ensurepip && pip install --no-cache-dir uv

# App code
WORKDIR /app
# Copy only dependency files first for better layer caching
COPY server/pyproject.toml server/uv.lock* /app/server/
# Sync deps into system site-packages (no venv inside container)
RUN uv pip install --system -r /app/server/pyproject.toml || uv sync --no-install-project

# Copy the rest of the app
COPY server/ /app/server/

# If you rely on optional OpenAI summarization, ensure the client is present
# (safe to fail if not pinned in pyproject)
RUN uv pip install --system openai || true

# Network/port
WORKDIR /app/server
ENV PORT=8080
EXPOSE 8080

# Healthcheck hits your /healthz route
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

# Start the HTTP MCP server (serves /mcp and /healthz)
CMD ["python", "server.py"]
