# Map design notes

Foundry's own summary. The ideas come from the map-design research in SyphoDev's free Roblox skills
(https://github.com/SyphoDev-yt/roblox-ai-skills, `skills/roblox-map/references/map-design.md`,
credit: the SyphoDev YouTube channel) and the DevForum guides it cites. Numbers are rules of thumb,
not engine limits: measure in your own game.

## Layout
- Decide the points of interest (POIs) first; the map outline follows from them.
- Connect POIs so there is more than one route to most of them (think in triangles, not a single chain).
- Keep POIs within about 40 seconds of walking of each other (about 640 studs at a 16 studs/s walk) as an
  upper bound. Busy hub or farming maps want them much closer (8-16 s).
- Draw an area around each POI; where two areas overlap is where secondary content goes (a camp, a well,
  a cabin).
- Order of work: block out POIs, draw zone borders, lay the main paths, place secondary content, walk it
  for travel time, add side paths, then dress.

## Readability
- A landmark is distinct and has height. Corners and turns are the best places for one.
- Each zone has its own palette and materials.
- Main paths are broad with a wide entrance; a narrow entrance reads as "not this way".
- Height gives vantage points, landmarks, cover and focus at once.
- Cover hides information but should not make a player permanently safe.
- Avoid the strict grid in decoration: rotate, scale and offset repeated pieces, curve paths.
- Decorate the outer edges (trees, rocks, plants); bare edges look unfinished.

## Lighting looks
- Shaded stylized: keep shadows; Atmosphere density around 0.1-0.14 for a small map (0.3+ fogs it out),
  haze 0.2-0.3, colour-correction tint near white, morning or late-afternoon clock time for long warm
  shadows.
- Fully flat cartoon: brightness 0, white ambient and outdoor ambient, global shadows off, parts with
  `CastShadow = false`. A different art direction: choose one, do not mix.

## Water
- Use Terrain water (`Terrain:FillBlock(cf, size, Enum.Material.Water)`); a transparent part has no waves,
  reflection or swimming.
- Terrain water does not show through a solid part: cut a real hole in the ground around it.
- Terrain is a 4-stud voxel grid: overlapping rock and water fills fight voxel by voxel. End the basin
  where the water starts.
- Engine water defaults suit deep ocean; ponds want lower reflectance and smaller, slower waves.

## Buildings
- A roof overhangs the wall by 1-2 studs, and the shingle course by another stud; a flush roof looks
  unfinished.
- Walls about 0.7 studs thick; window sills about a character's mid-height.
