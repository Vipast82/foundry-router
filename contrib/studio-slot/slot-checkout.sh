#!/bin/bash
# Put the slot's AU checkout (~/work/AnimalUprising) at a commit or branch from its origin (the
# mounted AU checkout) and get Studio onto it:
#   slot-checkout <ref>        e.g. a sha, master, track/as-d19
# - fetches origin, checks out <ref> detached (the slot never commits);
# - wally install when wally.lock changed (Packages/ is not committed);
# - Rojo live-syncs edits, but Rojo 7.7 misses new files and project-file changes
#   (rojo-rbx/rojo#1300): when the move adds files or touches default.project.json, Rojo is
#   restarted and Studio reopened (Rojo reconnects and syncs on open, no prompt).
# Prints "SLOT-CHECKOUT <sha> OK" when Studio is ready to be gated.
set -euo pipefail
ref="${1:?usage: slot-checkout <ref>}"
CO="${AU_CHECKOUT:-$HOME/work/AnimalUprising}"
export PATH="$HOME/.rokit/bin:$PATH"
# a rotated key reaches the slot before the gate (unchanged = one cheap 304); never fails the move
slot-secrets >&2 || true
# a new slot host: its game mirror was empty when the container started, so the first gate
# clones the game here (the mirror now has the track's commit)
first=0
if [ ! -d "$CO/.git" ]; then
    mkdir -p "$(dirname "$CO")"
    git clone -q --no-checkout "${AU_SOURCE:-/work/AnimalUprising}" "$CO"
    first=1
fi
cd "$CO"
git fetch -q origin '+refs/heads/*:refs/remotes/origin/*'
target=$(git rev-parse --verify -q "origin/$ref^{commit}" || git rev-parse --verify "$ref^{commit}")
old=$(git rev-parse -q --verify HEAD || true)
if [ "$first" = 1 ] || [ -z "$old" ]; then
    git -c advice.detachedHead=false checkout -q --force "$target"
    echo "slot-checkout: first checkout of the game: installing its toolchain and packages"
    if [ -f rokit.toml ]; then
        rokit self-install >/dev/null 2>&1 || true
        rokit install --no-trust-check >/dev/null
    fi
    [ -d Packages ] || [ ! -f wally.toml ] || wally install >/dev/null
    pkill -f '[r]ojo serve' || true              # entrypoint.sh (re)starts it on the new project
    sleep 3
    studio-reopen --wait
    echo "SLOT-CHECKOUT $target OK (first checkout)"
    exit 0
fi
if [ "$old" = "$target" ]; then
    echo "SLOT-CHECKOUT $target OK (already there)"
    exit 0
fi
added=$(git diff --name-only --diff-filter=AR "$old" "$target" | wc -l)
proj=$(git diff --name-only "$old" "$target" -- default.project.json | wc -l)
lock=$(git diff --name-only "$old" "$target" -- wally.lock wally.toml | wc -l)
git -c advice.detachedHead=false checkout -q --force "$target"
git clean -q -fd -e Packages -e ServerPackages
[ "$lock" -gt 0 ] && wally install >/dev/null
if [ "$added" -gt 0 ] || [ "$proj" -gt 0 ]; then
    echo "slot-checkout: $added new file(s), project file changed: $proj -> restarting Rojo + reopening Studio"
    pkill -f '[r]ojo serve' || true          # entrypoint.sh restarts it
    sleep 3
    studio-reopen --wait
else
    sleep 3                                   # Rojo pushes the edits to Studio
fi
echo "SLOT-CHECKOUT $target OK"
