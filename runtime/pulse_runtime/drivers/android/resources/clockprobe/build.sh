#!/bin/sh
# Rebuild pulse-clock.jar (dex) from ClockProbe.java. Requires JDK 17+ and Android build-tools.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
sdk=${ANDROID_HOME:-$HOME/Library/Android/sdk}
bt=$(ls -d "$sdk"/build-tools/* | sort -V | tail -1)
tmp=$(mktemp -d)
javac --release 11 -d "$tmp" "$here/ClockProbe.java"
"$bt/d8" --min-api 26 --output "$tmp/out.zip" "$tmp/ClockProbe.class"
cp "$tmp/out.zip" "$here/../pulse-clock.jar"
rm -rf "$tmp"
shasum -a 256 "$here/../pulse-clock.jar"
