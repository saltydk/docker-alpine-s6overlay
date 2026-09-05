FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS builder

# renovate: datasource=custom.rarlab depName=unrar versioning=loose
ARG UNRAR_VER=7.2.7
ADD https://www.rarlab.com/rar/unrarsrc-${UNRAR_VER}.tar.gz /tmp/unrar.tar.gz
RUN apk add --no-cache build-base && \
    tar -xzf /tmp/unrar.tar.gz && \
    cd unrar && \
    sed -i 's|LDFLAGS=-pthread|LDFLAGS=-pthread -static|' makefile && \
    sed -i 's|CXXFLAGS=-march=native |CXXFLAGS=|' makefile && \
    make -f makefile && \
    install -Dm 755 unrar /usr/bin/unrar

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS runtime

ENV PUID="1000" PGID="1000" UMASK="002" TZ="Etc/UTC"
ENV XDG_CONFIG_HOME="/config/.config" XDG_CACHE_HOME="/config/.cache" XDG_DATA_HOME="/config/.local/share" LANG="C.UTF-8" LC_ALL="C.UTF-8"
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

VOLUME ["/config"]
ENTRYPOINT ["/init"]

# install packages
RUN apk upgrade --no-cache && \
    apk add --no-cache tzdata shadow bash curl wget jq grep sed coreutils findutils python3 unzip p7zip ca-certificates xz

COPY --from=builder /usr/bin/unrar /usr/bin/unrar

# make folders
RUN mkdir -p \
    /app \
    /config \
    /defaults && \
# create user
    useradd -u 1000 -U -d /config -s /bin/false abc && \
    usermod -G users abc

# https://github.com/just-containers/s6-overlay/releases
# renovate: datasource=github-releases depName=just-containers/s6-overlay extractVersion=^v(?<version>.*)$ versioning=loose
ARG S6_VERSION=3.2.3.2
ARG TARGETARCH
ARG TARGETVARIANT

# install s6-overlay
RUN \
  case "${TARGETARCH}/${TARGETVARIANT}" in \
    "amd64/") S6_ARCH="x86_64" ;; \
    "arm64/") S6_ARCH="aarch64" ;; \
    "arm/v7") S6_ARCH="arm" ;; \
    *) echo "Unsupported platform: ${TARGETARCH}/${TARGETVARIANT}" >&2; exit 1 ;; \
  esac && \
  cd /tmp && \
  curl -fsSLO "https://github.com/just-containers/s6-overlay/releases/download/v${S6_VERSION}/s6-overlay-noarch.tar.xz" && \
  curl -fsSLO "https://github.com/just-containers/s6-overlay/releases/download/v${S6_VERSION}/s6-overlay-noarch.tar.xz.sha256" && \
  curl -fsSLO "https://github.com/just-containers/s6-overlay/releases/download/v${S6_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" && \
  curl -fsSLO "https://github.com/just-containers/s6-overlay/releases/download/v${S6_VERSION}/s6-overlay-${S6_ARCH}.tar.xz.sha256" && \
  sha256sum -c s6-overlay-noarch.tar.xz.sha256 && \
  sha256sum -c "s6-overlay-${S6_ARCH}.tar.xz.sha256" && \
  tar -C / -Jxpf s6-overlay-noarch.tar.xz && \
  tar -C / -Jxpf "s6-overlay-${S6_ARCH}.tar.xz" && \
  rm -f s6-overlay-*.tar.xz s6-overlay-*.tar.xz.sha256

COPY root/ /
