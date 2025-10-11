# Use Python 3.10 slim
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

# Hardcoded GCS bucket (optional, if you want to use ENV)
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
    imagemagick \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ✅ Download and register Roboto font
RUN mkdir -p /usr/share/fonts/truetype/roboto && \
    curl -L "https://github.com/google/fonts/raw/main/apache/roboto/Roboto-Regular.ttf" \
    -o /usr/share/fonts/truetype/roboto/Roboto-Regular.ttf && \
    fc-cache -f -v

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install moviepy

# Copy app source
COPY . .

# Cloud Run entry point
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]
