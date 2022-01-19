FROM alpine:3.14

ENV PUID="1000" PGID="1000" UMASK="002" TZ="Etc/UTC"
ENV XDG_CONFIG_HOME="/config/.config" XDG_CACHE_HOME="/config/.cache" XDG_DATA_HOME="/config/.local/share" LANG="C.UTF-8" LC_ALL="C.UTF-8"
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

VOLUME ["/config"]
ENTRYPOINT ["/init"]

# install packages
RUN apk add --no-cache tzdata shadow bash curl wget jq grep sed coreutils findutils python3 unzip p7zip ca-certificates xz && \
    apk add --no-cache --repository http://dl-cdn.alpinelinux.org/alpine/v3.14/main unrar

# make folders
RUN mkdir -p \
    /app \
    /config \
    /defaults && \
# create user
    useradd -u 1000 -U -d /config -s /bin/false abc && \
    usermod -G users abc

# https://github.com/just-containers/s6-overlay/releases
ARG S6_VERSION=2.2.0.3

# install s6-overlay
RUN \
# Find arch for archive
  ARCH=$(uname -m) && \
  OVERLAY_ARCH="" && \
  [ "${ARCH}" = "x86_64" ] && OVERLAY_ARCH="amd64" || true && \
  [ "${ARCH}" = "aarch64" ] && OVERLAY_ARCH="aarch64" || true && \
  [ "${ARCH}" = "armv7l" ] && OVERLAY_ARCH="armhf" || true && \
  curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_VERSION}/s6-overlay-${OVERLAY_ARCH}.tar.gz" | tar xzf - -C /

COPY root/ /
