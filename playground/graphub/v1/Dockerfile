FROM cbm-validation-toolchain:latest
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gh jq ripgrep sqlite3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
