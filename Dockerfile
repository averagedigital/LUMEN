FROM node:22-bookworm@sha256:0e5f906573693feaa1e21057ebdcfdb5bd5021f050b2dc7c9deceb629c7da2a8
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv fonts-noto-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir uv==0.9.8
ENV PATH="/opt/venv/bin:${PATH}" PLAYWRIGHT_BROWSERS_PATH=/opt/browsers PYTHONUNBUFFERED=1
WORKDIR /app
COPY package*.json ./
RUN npm ci && npx playwright install --with-deps chromium
COPY pyproject.toml uv.lock ./
COPY python ./python
RUN uv export --frozen --no-hashes --output-file /tmp/requirements.txt && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt
COPY . .
USER node
CMD ["lumen-data", "doctor"]
