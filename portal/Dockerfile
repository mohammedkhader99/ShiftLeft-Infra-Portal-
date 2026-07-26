# Container image for the Infra Portal (the web UI).
# Same shape as the API image (ARCHITECTURE.md §3 — a separate service).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer is cached when only code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the portal application (code, templates, vendored static assets).
COPY portal ./portal

# The portal listens on 5173 (PLAN.md §2, increment 0.3).
EXPOSE 5173

CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "5173"]
