FROM python:3.11-slim
WORKDIR /app

# System dependencies for Pillow (thumbnail generation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    libwebp-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Download vendor JS assets — bundled into the image so the app works offline in LAN.
# The image is built once with internet access; after that no external connectivity needed.
RUN mkdir -p /app/app/static/vendor /app/app/static/fonts && \
    curl -fsSL "https://unpkg.com/htmx.org/dist/htmx.min.js"        -o /app/app/static/vendor/htmx.min.js  && \
    curl -fsSL "https://unpkg.com/alpinejs/dist/cdn.min.js"          -o /app/app/static/vendor/alpine.min.js && \
    curl -fsSL "https://cdn.tailwindcss.com"                         -o /app/app/static/vendor/tailwind.js

# Download JetBrains Mono (WOFF2) — served locally for LAN-only operation
RUN curl -fsSL \
      "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/webfonts/JetBrainsMono-Regular.woff2" \
      -o /app/app/static/fonts/JetBrainsMono-Regular.woff2 && \
    curl -fsSL \
      "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/webfonts/JetBrainsMono-Bold.woff2" \
      -o /app/app/static/fonts/JetBrainsMono-Bold.woff2

# Create image storage directory (overridden by Docker volume in production)
RUN mkdir -p /app/data/images

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
