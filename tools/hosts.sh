#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
#
# tools/hosts.sh OUT
#
# The extension host of every forgeext revision in hosts, built from source
# under OUT: OUT/host-<n>/build/forgeext for each, OUT/hostargs (the --host
# arguments of check.py and publish.py), and OUT/ffx (the first revision's
# tools/ffx, which builds the index). FORGEEXT_GIT names the repository to
# clone (default: GitHub's).
set -eu

[ $# -eq 1 ] || { echo "usage: tools/hosts.sh OUT" >&2; exit 2; }
OUT=$1
REPO=${FORGEEXT_GIT:-https://github.com/openglow-org/forgeext.git}
HOSTS=$(cd "$(dirname "$0")/.." && pwd)/hosts
mkdir -p "$OUT"
git clone --quiet "$REPO" "$OUT/forgeext"
n=0
: > "$OUT/hostargs"
for rev in $(sed 's/#.*//' "$HOSTS"); do
    # A branch is the clone's remote one; a commit or a tag is itself.
    git -C "$OUT/forgeext" rev-parse --verify --quiet "$rev^{commit}" > /dev/null || rev="origin/$rev"
    d="$OUT/host-$n"
    git -C "$OUT/forgeext" worktree add --quiet --detach "$d" "$rev"
    cmake -S "$d" -B "$d/build" -DCMAKE_BUILD_TYPE=Release > /dev/null
    cmake --build "$d/build" -j"$(nproc)" --target forgeext > /dev/null
    printf ' --host %s' "$d/build/forgeext" >> "$OUT/hostargs"
    [ "$n" -gt 0 ] || echo "$d/tools/ffx" > "$OUT/ffx"
    echo "host $n: forgeext $(git -C "$d" rev-parse --short HEAD) ($rev)"
    n=$((n + 1))
done
[ "$n" -gt 0 ] || { echo "hosts names no forgeext revision" >&2; exit 1; }
