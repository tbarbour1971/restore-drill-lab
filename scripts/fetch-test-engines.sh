#!/usr/bin/env bash
# Downloads only the pinned official Linux amd64 test binaries; never installs system-wide.
set -euo pipefail
dest="$(realpath -m "${1:?usage: fetch-test-engines.sh DEST}")"
mkdir -p "$dest"
cd "$dest"
curl --fail --location --silent --show-error --max-time 90 -o restic.bz2 \
  https://github.com/restic/restic/releases/download/v0.20.0/restic_0.20.0_linux_amd64.bz2
curl --fail --location --silent --show-error --max-time 90 -o kopia.tar.gz \
  https://github.com/kopia/kopia/releases/download/v0.23.1/kopia-0.23.1-linux-x64.tar.gz
sha256sum -c <<'HASHES'
0ccd9fa7885074a24603b051e01bf9bd8b5e45d6a1791e9d9887e9e9372107f7  restic.bz2
416d0f84a3dbb321a8b2d8f0997b1a0a6e915babe79ee76fa6e4d2bd1e1c5178  kopia.tar.gz
HASHES
bzip2 -dc restic.bz2 > restic
tar -xOzf kopia.tar.gz kopia-0.23.1-linux-x64/kopia > kopia
chmod 755 restic kopia
./restic version
./kopia --version
