FROM python:3.11-slim-bookworm

# Keeps Python from generating .pyc files and enables unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (layer cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install system libraries needed for Chromium
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browsers (needed for crawl4ai scraping)
RUN playwright install --with-deps chromium

# Copy source
COPY . .

# Create storage dir in the image (mounted over in docker-compose)
RUN mkdir -p /app/storage

# Expose FastAPI port
EXPOSE 8000

# Start Uvicorn — bot runs in webhook mode when BOT_WEBHOOK_URL is set
CMD ["uvicorn", "api.webhook:app", "--host", "0.0.0.0", "--port", "8000"]
