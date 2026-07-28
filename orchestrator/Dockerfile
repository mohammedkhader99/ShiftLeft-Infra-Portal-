# Container image for the mock orchestrator (increment 1.9).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Terraform, for real provisioning (plan in 2.6a; apply in 2.6b).
ARG TERRAFORM_VERSION=1.9.8
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/tf.zip \
    && unzip /tmp/tf.zip -d /usr/local/bin \
    && rm /tmp/tf.zip \
    && apt-get purge -y unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The orchestrator app plus the shared signing helper.
COPY orchestrator ./orchestrator
COPY common ./common

# Pre-download the Terraform providers at build time so runtime plans are fast
# (no ~3-minute provider download on the first real plan).
RUN cd orchestrator/terraform && terraform init -input=false

EXPOSE 9091

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "9091"]
