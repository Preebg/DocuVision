# On-prem DocuVision worker (Docling + Ollama pipeline)
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Docling pulls torch; install CPU wheels so the image stays lean.
# Vision inference runs on remote Ollama, not this container's GPU.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY docuvision ./docuvision
COPY run_worker.py .

ENV PYTHONUNBUFFERED=1 \
    DOCUVISION_DATA_ROOT=/data

VOLUME ["/data"]

CMD ["python", "run_worker.py"]
