ARG TAG=
FROM alpine:${TAG}

# Args
ARG TAG
ARG OVERLAY_ARCH
ARG OVERLAY_VERSION="v1.22.1.0"

# Labels
LABEL VERSION="Alpine ${TAG}"

# Environment variables
ENV \
  PS1="$(whoami)@$(hostname):$(pwd)\\$ " \
  HOME="/root" \
  TERM="xterm"

RUN \
  echo "**** Building Alpine ${TAG} ****" && \
  echo "**** Install build packages ****" && \
  apk add --no-cache -U --virtual=build-dependencies \
    tar && \
  echo "**** Install runtime packages ****" && \
  apk add --no-cache -U \
    bash \
    ca-certificates \
    coreutils \
    curl \
    shadow \
    tzdata \
    xz && \
  echo "**** Add s6 overlay ****" && \
  curl -o \
  /tmp/s6-overlay.tar.gz -L \
    "https://github.com/just-containers/s6-overlay/releases/download/${OVERLAY_VERSION}/s6-overlay-${OVERLAY_ARCH}.tar.gz" && \
  tar xfz \
    /tmp/s6-overlay.tar.gz -C / && \
  echo "**** Create abc user and make our folders ****" && \
  groupmod -g 1000 users && \
  useradd -u 911 -U -d /config -s /bin/false abc && \
  usermod -G users abc && \
  mkdir -p \
    /app \
    /config \
    /defaults && \
  echo "**** Cleanup ****" && \
  apk del --purge \
    build-dependencies && \
  rm -rf \
    /tmp/*

# Add extra files
COPY root/ /

ENTRYPOINT ["/init"]
