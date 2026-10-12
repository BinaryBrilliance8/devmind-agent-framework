FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEVMIND_HOST=0.0.0.0 \
    DEVMIND_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 devmind

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY devmind ./devmind
RUN python -m pip install --no-cache-dir .

USER devmind
EXPOSE 8000
CMD ["devmind"]
