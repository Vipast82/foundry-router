// Studio plugin settings a slot needs, written while Studio is closed (Studio keeps these
// in memory and writes its copy back):
// - Rojo auto-reconnect + sync without asking (same as the Foundry Client's
//   studio.rojo_auto_sync); never_place_ids lose their saved Rojo connection
// - Studio as an MCP server (Assistant-ExternalMCPEnabled), which Roblox's StudioMCP.exe
//   (the gate's "official" MCP) talks to
//   node studio-plugin-settings.js <Roblox dir> [never place id ...]
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const never = new Set(process.argv.slice(3).map(String));
const want = { Rojo_autoReconnect: true, Rojo_confirmationBehavior: "Never",
  "Assistant-ExternalMCPEnabled": true };
let dirs = [];
try { dirs = fs.readdirSync(root).filter((d) => /^\d+$/.test(d)); } catch { process.exit(0); }
for (const uid of dirs) {
  const f = path.join(root, uid, "InstalledPlugins", "0", "settings.json");
  let data;
  try { data = JSON.parse(fs.readFileSync(f, "utf8").replace(/^﻿/, "")); } catch { continue; }
  if (!data || typeof data !== "object" || Array.isArray(data)) continue;
  const before = JSON.stringify(data);
  Object.assign(data, want);
  const prior = data.Rojo_priorEndpoints;
  if (prior && typeof prior === "object") for (const k of Object.keys(prior)) if (never.has(k)) delete prior[k];
  if (JSON.stringify(data) !== before) {
    fs.writeFileSync(f + ".tmp", JSON.stringify(data));
    fs.renameSync(f + ".tmp", f);
    console.log("studio plugin settings set in " + f);
  }
}
