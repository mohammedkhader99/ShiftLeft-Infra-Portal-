# Stage 1 — build the React + Carbon SPA (node base image is cached locally).
FROM node:22-alpine AS build
WORKDIR /frontend
COPY frontend/package.json ./
# Bump CACHEBUST (or pass --build-arg CACHEBUST=$(date +%s)) to force a clean
# npm install without re-pulling the base image (BuildKit's --no-cache can't,
# since Docker Hub is unreachable here).
ARG CACHEBUST=2
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2 — FastAPI Backend-for-Frontend that serves the SPA and proxies /api.
FROM python:3.12-slim
WORKDIR /app
COPY bff/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bff/ ./bff/
COPY --from=build /frontend/dist ./dist
ENV SPA_DIST=/app/dist
EXPOSE 5174
CMD ["uvicorn", "bff.main:app", "--host", "0.0.0.0", "--port", "5174"]
