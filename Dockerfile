# syntax=docker/dockerfile:1

# One image for both deployments: the Home Assistant app (configured through
# /data/options.json) and standalone Docker (configured through MEMTIER_*
# environment variables). The collector is the unchanged python/memtier.py;
# container/memtier_app.py adds the schedule, the web interface and the Home
# Assistant entities. Standard library only, so there is nothing to install.
FROM python:3.14-alpine

ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="vmware-memory-report" \
      org.opencontainers.image.description="Hourly VMware memory tiering collector with a self-contained trend report" \
      org.opencontainers.image.source="https://github.com/steiner-dominik/vmware-memory-report" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# tzdata: the log follows TZ (standalone). ca-certificates: the system trust
# store for vCenter certificates signed by a public CA.
RUN apk add --no-cache ca-certificates tzdata \
 && addgroup -g 1000 -S memtier \
 && adduser -u 1000 -S -G memtier memtier \
 && mkdir -p /data \
 && chown memtier:memtier /data

WORKDIR /opt/memtier
# Same layout as the repository, so memtier.py finds ../template on its own.
COPY python/memtier.py python/
COPY template/ template/
COPY container/ container/

# Only the version. No ENV defaults for paths or options on purpose: a variable
# set here would be indistinguishable from one the operator set.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEMTIER_APP_VERSION=${VERSION}

EXPOSE 8080

HEALTHCHECK --interval=2m --timeout=15s --start-period=30s --retries=2 \
    CMD ["python3", "/opt/memtier/container/memtier_app.py", "healthcheck"]

# No USER on purpose. The Home Assistant Supervisor writes the app's
# configuration to /data as root and does not change the container's user, so
# an image that drops privileges here cannot read its own configuration.
#
# The standalone deployment does not run as root: compose.yaml pins user
# 1000:1000, which owns /data in this image (and so a fresh named volume).
ENTRYPOINT ["python3", "/opt/memtier/container/memtier_app.py"]
CMD ["serve"]
