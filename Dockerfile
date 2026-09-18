ARG ALPINE_IMAGE=alpine:3.24@sha256:5b02b42e375f7426f8d65c3af331ca05d9878f9989230354504e0b9dfd431f60

# renovate: datasource=custom.rarlab depName=unrar versioning=loose
ARG UNRAR_VER=7.2.7
# https://github.com/just-containers/s6-overlay/releases
# renovate: datasource=github-releases depName=just-containers/s6-overlay extractVersion=^v(?<version>.*)$ versioning=loose
ARG S6_VERSION=3.2.3.2

FROM ${ALPINE_IMAGE} AS builder

ARG UNRAR_VER
COPY --chmod=0755 scripts/apk-lock.sh /usr/local/libexec/apk-lock
COPY packages/builder/ /tmp/apk-locks/

ADD https://www.rarlab.com/rar/unrarsrc-${UNRAR_VER}.tar.gz /tmp/unrar.tar.gz
RUN mkdir -p /usr/share/image-inputs && \
    cp "/tmp/apk-locks/$(apk --print-arch).lock" /usr/share/image-inputs/builder.lock && \
    /usr/local/libexec/apk-lock install /usr/share/image-inputs/builder.lock && \
    tar -xzf /tmp/unrar.tar.gz && \
    cd unrar && \
    sed -i 's|LDFLAGS=-pthread|LDFLAGS=-pthread -static|' makefile && \
    sed -i 's|CXXFLAGS=-march=native |CXXFLAGS=|' makefile && \
    make -f makefile && \
    install -Dm 755 unrar /usr/bin/unrar && \
    /usr/local/libexec/apk-lock verify /usr/share/image-inputs/builder.lock

FROM ${ALPINE_IMAGE} AS runtime

ENV PUID="1000" PGID="1000" UMASK="002" TZ="Etc/UTC"
ENV XDG_CONFIG_HOME="/config/.config" XDG_CACHE_HOME="/config/.cache" XDG_DATA_HOME="/config/.local/share" LANG="C.UTF-8" LC_ALL="C.UTF-8"
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

VOLUME ["/config"]
ENTRYPOINT ["/init"]

COPY --chmod=0755 scripts/apk-lock.sh /usr/local/libexec/apk-lock
COPY packages/runtime/ /tmp/apk-locks/
COPY --from=builder /usr/share/image-inputs/builder.lock /usr/share/image-inputs/builder.lock

RUN cp "/tmp/apk-locks/$(apk --print-arch).lock" /usr/share/image-inputs/runtime.lock && \
    /usr/local/libexec/apk-lock install /usr/share/image-inputs/runtime.lock

COPY --from=builder /usr/bin/unrar /usr/bin/unrar

# make folders
RUN mkdir -p \
    /app \
    /config \
    /defaults && \
# create user
    useradd -u 1000 -U -d /config -s /bin/false abc && \
    usermod -G users abc

ARG S6_VERSION
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
  rm -f s6-overlay-*.tar.xz s6-overlay-*.tar.xz.sha256 && \
  /usr/local/libexec/apk-lock verify /usr/share/image-inputs/runtime.lock

COPY root/ /

ARG ALPINE_IMAGE
ARG UNRAR_VER
ARG APK_LOCKS_SHA256
ARG IMAGE_INPUTS_SHA256

LABEL org.opencontainers.image.base.name="${ALPINE_IMAGE}" \
      io.saltydk.unrar.version="${UNRAR_VER}" \
      io.saltydk.s6.version="${S6_VERSION}" \
      io.saltydk.apk-locks.sha256="${APK_LOCKS_SHA256}" \
      io.saltydk.image-inputs.sha256="${IMAGE_INPUTS_SHA256}"
