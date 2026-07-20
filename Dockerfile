# ============================================================
# Ahad Co — RunSpace: SINGLE-SERVICE image (web + embedded job runner)
# ------------------------------------------------------------
# One Render web service does everything:
#   • the site (auth, dashboard, code studio, admin console)
#   • the job runner (spawn/auto-install/restart/log streaming) IN-PROCESS
#     (activates automatically when RUNNER_SERVICE_URL is unset)
#   • the public /live/{slug}/ gateway (HTTP + WebSocket)
#
# The classic two-service layout is still available (see runner/Dockerfile);
# this image is the default deployment target.
#
# Base + toolchain mirror runner/Dockerfile so every language users can
# deploy (python, node, bash, ruby, php, perl, lua, java, go, rust) runs
# identically in both layouts.
# ============================================================

# Pin to Debian 12 (bookworm): the floating `python:3.11-slim` tag now points
# to Debian 13 (trixie), which removed the `openjdk-17-jdk-headless` package
# and breaks the build. Bookworm still ships OpenJDK 17.
FROM python:3.11-slim-bookworm

# System packages + language runtimes available to user jobs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ openjdk-17-jdk-headless ruby php-cli perl lua5.4 \
    sqlite3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js (LTS) via NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Go
RUN curl -fsSL https://go.dev/dl/go1.22.0.linux-amd64.tar.gz \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust (rustc only, minimal)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

# Python deps: the site + the runner engine (jobs/gateway code lives in runner/)
COPY requirements.txt /tmp/req-main.txt
COPY runner/requirements.txt /tmp/req-runner.txt
RUN pip install --no-cache-dir -r /tmp/req-main.txt -r /tmp/req-runner.txt

COPY . .

# Non-root user for the server (user code executes as this user via subprocess)
RUN useradd -m runner
USER runner

EXPOSE 8000

# Render injects $PORT (usually 10000); default 8000 for local docker runs.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
