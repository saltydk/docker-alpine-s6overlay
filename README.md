# Alpine with s6-overlay

This repository builds `saltydk/alpine-s6overlay` for `linux/amd64`,
`linux/arm64`, and `linux/arm/v7`. The runtime includes s6-overlay, a static
UnRAR binary, common command-line tools, and the `abc` service account used by
downstream images.

## Package locks

Every installed Alpine package is pinned exactly for each architecture. The
`builder` profile contains the packages used to compile UnRAR; the `runtime`
profile contains the packages shipped in the final image. Human-maintained
requests live in `packages/<profile>/requested.txt`, while generated complete
inventories live in `packages/<profile>/<apk-architecture>.lock`.

The image provides `/usr/local/libexec/apk-lock` for downstream images. Its
`resolve` command can freeze all packages inherited from this image while
solving requested additions. Its `install` and `verify` commands enforce the
lock architecture and the complete installed inventory. The final image keeps
its selected builder and runtime locks under `/usr/share/image-inputs/` for
build reporting.

To validate committed metadata without checking for newer packages, run:

```shell
python3 scripts/manage_inputs.py verify
```

To resolve the current Alpine release-line digest and all six package locks,
run in an environment with Docker execution for all three target platforms:

```shell
python3 scripts/manage_inputs.py update
python3 scripts/manage_inputs.py update --write
```

The first command is a dry run. The second writes changes only after every
profile and platform resolves successfully. Updating the Alpine release line
itself is an explicit Dockerfile change; automatic updates retain the selected
major/minor tag.

## Build

The build requires the lock and image-input digests exposed by the management
script. CI computes these values and passes them as `APK_LOCKS_SHA256` and
`IMAGE_INPUTS_SHA256` build arguments. During each build, both stages install
the complete exact lock and verify their resulting package inventories before
the image can be published.
