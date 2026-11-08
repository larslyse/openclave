# OpenClave brand

The mascot is **the Keeper**: a mint hooded figure with two eyes. A conclave is a closed gathering in hooded robes, and the Keeper watches over your agents.

| File | Use |
|---|---|
| `svg/keeper.svg` | The mascot, transparent background. Site header, README, avatars. |
| `svg/keeper-small.svg` | 32 px and below (no rim, seam or eyes). |
| `svg/keeper-thinking.svg`, `keeper-done.svg`, `keeper-error.svg` | Poses for working / finished / something went wrong. |
| `svg/app-icon.svg` | Phone and PWA icon, full-bleed square (the phone rounds the corners). |
| `svg/favicon.svg` | Browser tab icon on a dark rounded tile. |
| `tokens.css` | Colours and fonts. |

Colours: mint `#7fd1b0` (accent), deep mint `#5fae90`, night `#111416` (background), panel `#1a1e22`, line `#2b3137`, paper `#f0ede7` (text), muted `#969ca3`, danger `#ef8b83`.
Fonts: Space Grotesk for headings, Inter for body text.

Edit the SVGs here, never the copies, then run `brand/render.sh`. It renders the PNG icons with headless Chromium and copies the files into `site/` and `static/mobile/icons/`. After changing the app icon, bump the `?v=` number in `static/mobile/index.html`, `manifest.webmanifest` and `notification-sw.js` so phones pick it up.
