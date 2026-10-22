#!/usr/bin/env bash
# Build FoundryClient-<version>-x86_64.AppImage from the PyInstaller bundle.
#   bash packaging/linux/build_appimage.sh 0.133.0      (run from the repo root, after
#   `pyinstaller packaging/foundry-client.spec --noconfirm`)
set -euo pipefail
VER="${1:?version}"
ROOT="$(pwd)"
APPDIR="$ROOT/dist/FoundryClient.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$ROOT/dist/FoundryClient" "$APPDIR/usr/lib/foundry-client"
python - <<'PY'
from foundry_client.tray import icon_image
icon_image("ok", 256).save("dist/FoundryClient.AppDir/foundry-client.png")
PY
cp "$APPDIR/foundry-client.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/foundry-client.png"
cat > "$APPDIR/foundry-client.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Foundry Client
Comment=Runs the Foundry automation stack (Kilo, coordinator, Rojo) with a tray icon
Exec=FoundryClient run
Icon=foundry-client
Categories=Development;
Terminal=false
DESK
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
case "$1" in
  run|"") exec "$HERE/usr/lib/foundry-client/FoundryClient" "${@:-run}" ;;
  *)      exec "$HERE/usr/lib/foundry-client/foundry-client" "$@" ;;
esac
RUN
chmod +x "$APPDIR/AppRun"
if ! command -v appimagetool >/dev/null; then
  curl -fsSL -o /tmp/appimagetool https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x /tmp/appimagetool
  TOOL=/tmp/appimagetool
else
  TOOL=appimagetool
fi
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$ROOT/dist/FoundryClient-$VER-x86_64.AppImage"
echo "built dist/FoundryClient-$VER-x86_64.AppImage"
