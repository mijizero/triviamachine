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

# Ensure fontconfig can find custom fonts
RUN fc-cache -f -v

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
# Install MoviePy explicitly if not already in requirements
RUN pip install moviepy

# Copy app source including credentials.json
COPY Roboto-Regular.ttf /app/
COPY . .

# Run with Gunicorn (Cloud Run friendly)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]
