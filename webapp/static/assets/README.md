# Drop-in art slot

The workshop renders with **procedurally-generated pixel art** by default, so it
works fully offline with zero binary assets. This folder is an optional **drop-in
slot**: add real tile art here and the floor tiles upgrade automatically — no code
change.

## How it works

On boot, `game.js` tries to load `assets/manifest.json`. If it's absent (the
default), nothing happens and the procedural tiles are used. If it's present, the
`tiles` map overrides the floor texture for each named room with your PNG.

## To add art

1. Drop tileable PNGs into this folder (any size; they're stretched to the floor
   area and rendered with `image-rendering: pixelated`, so 16×16 / 32×32 tiles
   look crisp).
2. Copy `manifest.example.json` to `manifest.json` and edit the `tiles` map.
3. Reload — the named rooms now use your art; unnamed rooms stay procedural.

Room keys: `reception`, `product`, `architecture`, `engineering`, `qa`, `gate`,
`vault`.

CC0 packs that fit the top-down look (download on your machine; the sandbox can't
fetch them): Kenney "Tiny Town" / "RPG Urban" / "Roguelike" (kenney.nl). Keep the
license file alongside the art if the pack requires attribution.

## Notes

- The override is **best-effort**: a missing or broken file degrades silently back
  to procedural — the app never breaks on bad art.
- This is view-only. Like everything in the workshop, art cannot influence a build;
  the signed gates decide.
