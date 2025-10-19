# Use full Debian-based Python image for easier Aeneas build
FROM python:3.10-bullseye

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV OUTPUT_BUCKET=trivia-videos-output

# Install Aeneas and multimedia build dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    espeak \
    libespeak-dev \
    libxml2-dev \
    libxslt-dev \
    git \
    libavcodec-extra \
    fonts-dejavu-core \
    fontconfig \
    libsm6 \
    libxext6 \
    build-essential \
    libatlas-base-dev \
    libffi-dev \
    libsndfile1-dev \
    python3-dev \
    python3-distutils \
    sox \
    imagemagick \
    wget \
    curl \
 && rm -rf /var/lib/apt/lists/*

# ✅ Download and register Roboto font (using curl for reliability)
RUN mkdir -p /usr/share/fonts/truetype/roboto && \
    curl -L "https://github.com/google/fonts/raw/main/apache/roboto/Roboto-Regular.ttf" \
    -o /usr/share/fonts/truetype/roboto/Roboto-Regular.ttf && \
    fc-cache -f -v

# Upgrade pip and build tools
RUN python3 -m pip install --upgrade pip setuptools wheel

# ✅ Fix Aeneas build compatibility for Python 3.10
RUN pip install setuptools==58.0.4 numpy==1.23.0

# 🩵 Install Aeneas after numpy and distutils are ready
RUN pip install aeneas==1.7.3.0

# Set working directory
WORKDIR /app

# Copy requirements early for caching efficiency
COPY requirements.txt .

# ✅ Install project dependencies efficiently
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install moviepy

# Copy app source including Roboto font and credentials
COPY Roboto-Regular.ttf /app/
COPY . .

# ✅ Run with Gunicorn (Cloud Run friendly)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=2", "--threads=4", "--timeout=0"]
