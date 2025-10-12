# Use Python base image
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV OUTPUT_BUCKET=trivia-videos-output

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-pip \
    espeak \ 
    libxml2-dev \ 
    libxslt-dev \ 
    python3-dev \ 
    git \ 
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
    && pip install numpy aeneas \ 
    && rm -rf /var/lib/apt/lists/*

# ✅ Download and register Roboto font (using curl for reliability)
RUN mkdir -p /usr/share/fonts/truetype/roboto && \
    curl -L "https://github.com/google/fonts/raw/main/apache/roboto/Roboto-Regular.ttf" \
    -o /usr/share/fonts/truetype/roboto/Roboto-Regular.ttf && \
    fc-cache -f -v

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install moviepy

# Copy app source including credentials.json
COPY Roboto-Regular.ttf /app/
COPY . .

# Run with Gunicorn (Cloud Run friendly)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]
