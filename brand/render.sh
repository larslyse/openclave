#!/usr/bin/env bash
# Renders the PNG icons from brand/svg and copies the brand files to where the site and app load them.
# Needs chromium (headless). Run from anywhere: brand/render.sh
set -euo pipefail
cd "$(dirname "$0")"
repo=..
out=$(mktemp -d "$PWD/.render-XXXX")
trap 'rm -rf "$out"' EXIT
browser=$(command -v chromium || command -v chromium-browser || command -v google-chrome)

png() { # png <svg> <size> <dest>
  printf '<html><body style="margin:0;background:transparent"><img src="%s" width="%s" height="%s" style="display:block"></body></html>' \
    "file://$PWD/svg/$1" "$2" "$2" > "$out/page.html"
  "$browser" --headless --disable-gpu --hide-scrollbars --default-background-color=00000000 \
    --window-size="$2,$2" --screenshot="$out/shot.png" "file://$out/page.html" >/dev/null 2>&1
  mv "$out/shot.png" "$3"
}

icons=$repo/static/mobile/icons
for s in 180 192 512; do png app-icon.svg $s "$icons/app-icon-$s.png"; done
cp svg/app-icon.svg "$icons/app-icon.svg"
cp svg/favicon.svg "$icons/favicon.svg"
cp svg/keeper.svg "$icons/keeper.svg"

cp svg/favicon.svg "$repo/site/favicon.svg"
cp svg/keeper.svg "$repo/site/keeper.svg"
png app-icon.svg 180 "$repo/site/apple-touch-icon.png"
cp tokens.css "$repo/site/tokens.css"
echo "Rendered icons and copied brand files."
