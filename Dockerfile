FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Copy the server code
COPY server/ /app/

# Install runtime deps
RUN pip install --no-cache-dir \
    "mcp[server]" \
    httpx \
    python-dotenv \
    uvicorn \
    starlette \
 && pip install --no-cache-dir openai || true

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

CMD ["python", "server.py"]
