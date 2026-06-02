FROM python:3.12-slim

WORKDIR /app

ARG ORCHESTRATOR_GIT_SHA=unknown
ARG ORCHESTRATOR_BUILD_TIME=unknown
ARG ORCHESTRATOR_IMAGE_TAG=unknown
ARG ORCHESTRATOR_IMAGE_ID=unknown

ENV ORCHESTRATOR_GIT_SHA=${ORCHESTRATOR_GIT_SHA}
ENV ORCHESTRATOR_BUILD_TIME=${ORCHESTRATOR_BUILD_TIME}
ENV ORCHESTRATOR_IMAGE_TAG=${ORCHESTRATOR_IMAGE_TAG}
ENV ORCHESTRATOR_IMAGE_ID=${ORCHESTRATOR_IMAGE_ID}

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    nodejs \
    npm \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8080

# Run with uvicorn
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]
