# servmon collector: Python + OpenSSH client + cloudflared (for hosts reached
# through a Cloudflare tunnel ProxyCommand). Built by compose.yaml.
ARG CLOUDFLARED_VERSION=2026.11.0
FROM docker.io/cloudflare/cloudflared:${CLOUDFLARED_VERSION} AS cloudflared

FROM docker.io/library/python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

# ssh refuses to run for a uid without a passwd entry, and checks that ~/.ssh is
# owned by the user, so the container user mirrors the host user's uid/gid.
ARG UID=1000
ARG GID=1000
RUN groupadd -o -g "$GID" servmon && useradd -o -m -u "$UID" -g "$GID" -s /bin/sh servmon

WORKDIR /servmon
COPY servmon/ servmon/
USER servmon
ENV PYTHONUNBUFFERED=1 SERVMON_LISTEN=0.0.0.0:9877
CMD ["python3", "servmon/collector.py", "-c", "/servmon/config.toml"]
