# Pixie Hollow component V6 SWF evidence

Source repository: `PixieHollowRE/web`

Source commit: `5cd7f024ba7918f16975163e59a8320deb20dee3`

The following records were produced by JPEXS `swf2xml` directly from the indicated SWFs at that commit.

## Cozy Fireplace

Source SWF: `swf/winterStorage002.swf`

Exported root `Fireplace` is `DefineSprite` tag 11. Its direct placements are:

- tag 2, depth 1, instance `color2`, translation `(-12, 0)`
- tag 4, depth 77, instance `color1`, translation `(149, 171)`
- tag 6, depth 92, translation `(40, 221)`
- tag 10, depth 109, translation `(-53, 50)`

Inside tag 10:

- tag 7 at depth 71
- tag 9 at depth 72 via `PlaceObject3`, blend mode 2, translation `(-1274, -1134)`, and alpha multiplier `64/256`

The earlier graph parser matched only `PlaceObject2` records, so it omitted the tag 9 `PlaceObject3` branch when computing tag 10's registration bounds.

## River Rock

Source SWF: `swf/ms15Storage_001.swf`

Exported root `riverRock` is `DefineSprite` tag 88. Its first and only frame contains:

- tag 83 at depth 1, instance `color1`, normal placement, scale `0.12226868`, translation `(-487, -260)`
- tag 85 at depth 19, instance `color1`, `PlaceObject3`, blend mode 13, scale `0.12226868`, translation `(-6, -15)`, alpha multiplier `115/256`
- tag 87 at depth 21, instance `color1`, `PlaceObject3`, blend mode 13, scale `0.12226868`, translation `(-5, -52)`, alpha multiplier `154/256`

All three placements belong to the same named visual dye channel, `color1`. The depth-19 and depth-21 overlay layers produce the lighter top swirl/highlight visible in the completed sprite.

## Picnic Table

Source SWF: `swf/ms17Storage_002.swf`

Exported root `PicnicTable` is `DefineSprite` tag 41 and has two straightforward direct dye branches:

- tag 38, instance `color2`
- tag 40, instance `color1`

No missing `PlaceObject3` effect branch was found for this object.
