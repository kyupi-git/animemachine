FROM python:3.14-slim

ARG ANM_VERSION
LABEL org.opencontainers.image.title="AnimeMachine" \
      org.opencontainers.image.description="Automatic anime library manager" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/kyupi-git/animemachine" \
      org.opencontainers.image.url="https://github.com/kyupi-git/animemachine" \
      org.opencontainers.image.documentation="https://github.com/kyupi-git/animemachine/blob/main/docs/guide.md" \
      org.opencontainers.image.vendor="kyupi-git" \
      org.opencontainers.image.version=$ANM_VERSION

RUN apt-get update \
    && apt-get upgrade -y \
    && (apt-get install -y --no-install-recommends ca-certificates ffmpeg 7zip || apt-get install -y --no-install-recommends ca-certificates ffmpeg p7zip-full) \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 anm && useradd --uid 1000 --gid anm --create-home anm

WORKDIR /opt/animemachine
COPY pyproject.toml VERSION README.md LICENSE THIRD-PARTY.md ./
COPY src/ ./src/
COPY packaging/docker/entrypoint.py /opt/animemachine/docker-entrypoint.py
RUN python -m pip install --no-cache-dir --disable-pip-version-check . \
    && python -m pip uninstall --yes pip setuptools msgpack \
    && mkdir -p /Config /Data/state /Imports /Library /Torrents /External /Media /CollectorState \
    && chown -R anm:anm /Config /Data /Imports /Library /Torrents /External /Media /CollectorState /opt/animemachine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANM_INSTALL_ROOT=/opt/animemachine \
    ANM_INSTALL_MODE=docker \
    ANM_DOCKER_UPDATE_RUNTIME=1 \
    ANM_STATE_DIR=/Data/state \
    ANM_CONFIG_PATH=/Config/config.json \
    ANM_IMPORTS_DIR=/Imports
USER anm
EXPOSE 8787
ENTRYPOINT ["python", "/opt/animemachine/docker-entrypoint.py"]
CMD ["run"]
