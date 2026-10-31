#!/bin/bash
# Print "<placeId>=<projectName>" for studio-plugin-settings.js: the slot's own place (from
# STUDIO_OPEN) and the Rojo project it serves (the "name" in ROJO_PROJECT, default the slot's
# AU checkout). Rojo's auto-reconnect needs that pair saved, or a reopened Studio stays unsynced.
# Prints nothing when either half is unknown.
PID=$(printf '%s' "${STUDIO_OPEN:-}" | sed -n 's/.*placeId:\([0-9][0-9]*\).*/\1/p')
PROJ="${ROJO_PROJECT:-${AU_CHECKOUT:-$HOME/work/AnimalUprising}/default.project.json}"
NAME=$(node -e 'try { const n = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")).name;
  if (typeof n === "string" && n && !/\s/.test(n)) console.log(n) } catch {}' "$PROJ" 2>/dev/null)
[ -n "$PID" ] && [ -n "$NAME" ] && echo "$PID=$NAME"
exit 0
