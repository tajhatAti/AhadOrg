# ============================================================
# Code Execution Runner — Dockerfile
# Separate service for code execution (Service #2).
# Installs common language runtimes, then runs the FastAPI runner.
# ============================================================

# Pin to Debian 12 (bookworm): the floating `python:3.11-slim` tag now points
# to Debian 13 (trixie), which removed the `openjdk-17-jdk-headless` package
# and breaks the build. Bookworm still ships OpenJDK 17.
FROM python:3.11-slim-bookworm

# Install system packages + language runtimes
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ openjdk-17-jdk-headless ruby php-cli perl lua5.4 \
    sqlite3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (LTS) via NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Go
RUN curl -fsSL https://go.dev/dl/go1.22.0.linux-amd64.tar.gz \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Install Rust (rustc only, minimal)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user for the server (executions still happen as nobody via subprocess)
RUN useradd -m runner
USER runner

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
