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

Package refresh runs every six hours. A manual `ci` run with `refresh-packages`
enabled resolves and commits all six locks before building and publishing the
selected source. The workflow's concurrency group serializes these runs. The
updater skips rebuilding when resolved inputs already match the published image.

qBittorrent and Autoscan check the published base every six hours and adopt its
verified digest when needed. qBittorrent reports `waiting-for-base` when inherited
package pins prevent resolution, preserves its inputs and published images, and
retries on a later version check. To accelerate that process, run this repository's
manual package refresh, wait for successful publication, then run the consumer's
update workflow.

## Build

The build requires the lock and image-input digests exposed by the management
script. CI computes these values and passes them as `APK_LOCKS_SHA256` and
`IMAGE_INPUTS_SHA256` build arguments. During each build, both stages install
the complete exact lock and verify their resulting package inventories before
the image can be published.

Container security uses the released `saltyorg/github-actions` scan, snapshot,
and reporting actions. Ordinary CVEs appear in retained scanner reports and
tracked GitHub issues. CISA KEV findings remain blocking. Candidate runtime tests,
package inventory checks, and the automated upgrade paths remain required.

Published-image scans run on unchanged-input refreshes and after successful
publication. A complete current scan can resolve an automation-owned CVE issue;
missing reports or scanner errors preserve existing issues. Publication and
issue reconciliation share a queued concurrency group. Reporting failures remain
visible and cannot prevent an already accepted candidate from publishing.
