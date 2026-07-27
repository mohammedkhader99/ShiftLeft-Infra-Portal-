# Container image for the mock orchestrator (increment 1.9).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The orchestrator app plus the shared signing helper.
COPY orchestrator ./orchestrator
COPY common ./common

EXPOSE 9091

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "9091"]
