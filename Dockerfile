# Use Python base image
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-dejavu-core \
    libsm6 \
    libxext6 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source including credentials.json
COPY . .

# Ensure credentials.json is in /app
# (already copied by the line above)

# Run with Gunicorn (Cloud Run friendly)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]