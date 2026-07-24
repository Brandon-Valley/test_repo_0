# Pixie Hollow Component Debug V8 dye-order evidence

Source repository: `PixieHollowRE/web`

Source commit: `5cd7f024ba7918f16975163e59a8320deb20dee3`

## Light Bright Canopy - item 6663

Source SWF: `swf/ms16Storage_001.swf`

Exported root: `CanopyJewel`, DefineSprite tag 39.

Direct display-list placements:

- `color1`: tag 35 at depth 1
- `color2`: tag 37 at depth 17
- Untinted jewel and decorative overlay: tag 38 at depth 532

The jewel layer is therefore drawn after both dyeable branches. A flattened dye overlay placed above the completed image incorrectly covers those jewels. In the component view, redrawing the dyeable parent after all visible layers causes the same loss.

## Sunflower Loveseat - item 6513

Source SWF: `swf/home.swf`

Exported root: `Furniture13`, DefineSprite tag 162.

Direct display-list placements:

- `color1`: tag 160 at depth 1
- Untinted detailed flower-center layer: tag 161 at depth 3

The center detail must be drawn after the dyed `color1` branch. Drawing the dye parent last covers the detailed center.

## Repository-wide audit

The current object-component map contains 539 mapped exported objects with at least one higher-depth, non-dye direct branch above a dye branch. The V8 browser fix is therefore depth-aware and repository-wide rather than hard-coded for items 6663 and 6513.

## V8 rendering correction

- Dyeable leaf images inherit their `color1` or `color2` slot from the complete hierarchy path and are tinted in their original display-list position.
- A dyeable parent is not redrawn when one of its rendered descendants already represents the same dye branch.
- Any required fallback dye branch is inserted into the normal depth-sorted render list instead of being appended after all other layers.
- In flattened mode, higher-depth untinted component layers are redrawn above the CSS dye overlays at their original Flash depths. This restores jewels, cushion details, trim, highlights, and other non-dye artwork without changing the original technical mask files.
