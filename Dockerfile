# Knowledge System container.
FROM python:3.12-slim

# System deps that some converters/embedders benefit from.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package with the full extra set (api, convert, embed, vector, mcp).
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[all,dev]"

# Default vault location (mounted as a volume in compose).
# State dir is derived automatically as <KS_VAULT_PATH>/.knowledge.
ENV KS_VAULT_PATH=/vault

VOLUME ["/vault"]
EXPOSE 8000

ENTRYPOINT ["knowledge"]
CMD ["watch"]
