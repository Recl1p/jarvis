#!/bin/sh
# Write the files to give a friend, with the voice key filled in: the TrueNAS and ZimaOS app YAML, and compose.yaml +
# .env for any other Docker host. Share them privately (they unlock the built-in voices); nothing here is uploaded.
#
#   scripts/friend-files.sh <file holding the voice key> <output folder>
set -eu
key_file=${1:?usage: scripts/friend-files.sh KEY_FILE OUTPUT_DIR}
out=${2:?usage: scripts/friend-files.sh KEY_FILE OUTPUT_DIR}
root=$(cd "$(dirname "$0")/.." && pwd)

key=$(tr -d ' \r\n' < "$key_file")
case "$key" in
    ""|*[!A-Za-z0-9_-]*) echo "$key_file doesn't hold a voice key" >&2; exit 1 ;;
esac

umask 077
mkdir -p "$out/docker"
for platform in truenas zimaos; do
    sed "s|@JARVIS_VOICE_KEY@|$key|" "$root/install/$platform.yaml" > "$out/jarvis-$platform.yaml"
    grep -q "JARVIS_VOICE_KEY: \"$key\"" "$out/jarvis-$platform.yaml" || { echo "no key slot in $platform.yaml" >&2; exit 1; }
done
cp "$root/compose.yaml" "$out/docker/compose.yaml"
sed "s|^JARVIS_VOICE_KEY=\$|JARVIS_VOICE_KEY=$key|" "$root/.env.example" > "$out/docker/.env"
grep -q "^JARVIS_VOICE_KEY=$key\$" "$out/docker/.env" || { echo "no key slot in .env.example" >&2; exit 1; }
cp "$root/INSTALL.md" "$out/INSTALL.md"
echo "Wrote $out: jarvis-truenas.yaml, jarvis-zimaos.yaml, docker/compose.yaml + docker/.env, INSTALL.md"
