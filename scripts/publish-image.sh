#!/bin/sh
# Build the JARVIS image from a clean checkout of HEAD and push it to GitHub's container registry as <version>,
# <major>.<minor>, <major> and latest (the friends' YAML uses <major>).
#
#   docker login ghcr.io -u <github user>          (a token with write:packages), then
#   scripts/publish-image.sh 2.1.0                 DOCKER="sudo -n docker" where docker needs sudo
#
# The version must match launcher/jarvis_launcher/__init__.py. The GitHub workflow (.github/workflows/image.yml) does
# the same on a v<version> tag.
set -eu
version=${1:?usage: scripts/publish-image.sh VERSION}
D=${DOCKER:-docker}
IMAGE=${JARVIS_PUBLISH_IMAGE:-ghcr.io/recl1p/jarvis}
root=$(cd "$(dirname "$0")/.." && pwd)

case "$version" in *[!0-9.]*|"") echo "version must look like 2.1.0" >&2; exit 2 ;; esac
grep -q "JARVIS_VERSION = \"$version\"" "$root/launcher/jarvis_launcher/__init__.py" \
    || { echo "launcher/jarvis_launcher/__init__.py is not at $version" >&2; exit 1; }
git -C "$root" diff --quiet HEAD -- || { echo "uncommitted changes: commit them first" >&2; exit 1; }

minor=${version%.*}
major=${version%%.*}
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
git -C "$root" archive --format=tar HEAD | tar -xf - -C "$context"

$D build --build-arg JARVIS_LOCAL_VOICES=pocket \
    --label org.opencontainers.image.source=https://github.com/Recl1p/jarvis \
    --label org.opencontainers.image.version="$version" \
    --label org.opencontainers.image.revision="$(git -C "$root" rev-parse HEAD)" \
    --label "org.opencontainers.image.description=JARVIS: a dashboard, voice and browser desktop for Hermes Agent" \
    -t "$IMAGE:$version" "$context"
for tag in "$minor" "$major" latest; do
    $D tag "$IMAGE:$version" "$IMAGE:$tag"
done
for tag in "$version" "$minor" "$major" latest; do
    $D push "$IMAGE:$tag"
done
echo "Pushed $IMAGE:{$version,$minor,$major,latest}"
