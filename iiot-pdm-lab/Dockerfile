FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Compile the Sparkplug protobuf at build time so it's baked into the image.
RUN python -c "import common.sparkplug"

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
