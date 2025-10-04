# Use Python base image
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
# Set OUTPUT_BUCKET environment variable
ENV OUTPUT_BUCKET=trivia-videos-output

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libavcodec-extra \
    fonts-dejavu-core \
    fontconfig \
    libsm6 \
    libxext6 \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Verify font availability (debugging only, can be removed later)
RUN fc-list | grep DejaVuSans || true

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .
COPY Roboto-Regular.ttf
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source including credentials.json
COPY . .

# Run with Gunicorn (Cloud Run friendly)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]
