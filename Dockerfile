# ---------------------------------------------------------------------------
# Stage 1: build + obfuscate
#   - Installs deps
#   - Compiles all .py → .pyc (inline, via compileall -b)
#   - Deletes .py source so only bytecode ships
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev libpng-dev libtiff-dev \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendored packages (pyotp, qrcode) - no internet needed
COPY vendor/ /usr/local/lib/python3.12/site-packages/

# Copy source, then obfuscate: compile → remove .py
COPY app/ .
RUN python -m compileall -b -q . \
    && find . -name "*.py" -not -path "./__init__.py" -delete \
    && find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

RUN mkdir -p /app/data /app/data/thumbs

# ---------------------------------------------------------------------------
# Stage 2: lean runtime image (no build tools, no source)
# ---------------------------------------------------------------------------
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo libpng16-16 libtiff6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn

# Copy obfuscated app (only .pyc + templates + static, no .py source)
COPY --from=builder /app/ .

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
