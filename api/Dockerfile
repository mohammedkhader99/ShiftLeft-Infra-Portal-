# Container image for the Infra Portal API.
# Python 3.12 to match ARCHITECTURE.md §9. Slim base keeps the image small.
FROM python:3.12-slim

# Don't write .pyc files; stream logs straight out (no buffering).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer when only code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code (the API, the shared db package, and common utils).
COPY api ./api
COPY db ./db
COPY common ./common

# The agent's doctrine. `common/doctrine.py` reads it from the image root, so
# an image carrying `common` and NOT this file starts fine and raises on the
# first AI call. A test asserts this line exists beside every COPY of common.
COPY AGENT-DOCTRINE.md ./

# The API listens on 8081 (8080 is taken by Docker Desktop on the reviewer's machine).
EXPOSE 8081

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8081"]
