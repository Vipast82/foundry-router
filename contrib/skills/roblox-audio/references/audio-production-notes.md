# Audio production notes (Foundry)

Lessons from generating and reviewing a full game sound set (Animal Uprising, 2026-11): about 130 SFX,
music and ambience loops, produced with text-to-audio tools and checked by the owner in listening
rounds.

## Briefs
- Write one brief per sound: what it is, the material or creature, the mood, the duration window, and
  loop or one-shot. Reuse the project's sound bible wording.
- Size matters: large creatures need low, heavy sounds (an elephant or rhino in a high register reads
  as a small animal). Name the real-world reference ("like a dog barking", "real elephant trumpet").
- Ambience needs a mix, not one texture: a castle bed is armour clatter, a blacksmith's hammer, distant
  chatter, light birds, spread over the whole loop; a den is drips, bats, squeaks, torch crackle.
  Generate the elements separately and mix them over the loop length.

## Automatic checks per take
- Duration inside the brief's window. Very short takes (well under half a second) usually fail; join
  two or three short calls with natural gaps, or ask for a series.
- True peak at or below about -1.5 dBTP (limit in post if needed); consistent loudness within a family.
- Loops: crossfade the loop point and check the seam; no click at start or end.
- Keep failed takes and the reason, so the next round changes the prompt or the post step instead of
  repeating it.

## Listening rounds
- Build a local page with one player per sound, keep/redo choices and a notes box, and a "copy picks"
  button that outputs one line per sound id. Record the owner's answer verbatim in the repo.
- Second and later rounds show only the redone sounds, old take next to new take.
- A redo note is the brief for the next take: copy it word for word into the generation request.
- Upload, register asset ids and wire only the winners; never a whole batch before the listen.
