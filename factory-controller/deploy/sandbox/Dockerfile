# Sandbox image for DockerSandbox (final draft §12.2 M8, §13.1 #2).
#
# Contains only what a task needs to run tests and builds: git, Python and
# Node. No credentials of any kind are baked in — the sandbox never mounts
# secrets, and by default it runs with --network none, so nothing here needs
# to reach out to anything. Runs as a non-root user; DockerSandbox also
# passes --user 1000:1000 at `docker run` time, so this UID exists to match
# ownership of the bind-mounted workdir.
FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-pip \
        python3-venv \
        nodejs \
        npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox

USER sandbox
WORKDIR /work

CMD ["/bin/bash"]
