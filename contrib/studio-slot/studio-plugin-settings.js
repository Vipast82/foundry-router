// Studio plugin settings a slot needs, written while Studio is closed (Studio keeps these
// in memory and writes its copy back):
// - Rojo auto-reconnect + sync without asking (same as the Foundry Client's
//   studio.rojo_auto_sync); never_place_ids lose their saved Rojo connection
// - Studio as an MCP server (Assistant-ExternalMCPEnabled), which Roblox's StudioMCP.exe
//   (the gate's "official" MCP) talks to
// - Rojo's saved endpoint for the slot's own place (<placeId>=<projectName>): the plugin's
//   auto-reconnect only reconnects a place whose priorEndpoints entry names the project
//   (App:tryAutoReconnect), and Studio dropped that entry on 2026-10-31, after which every
//   reopen came back unsynced (stale code in the slot until someone clicked Connect)
//   node studio-plugin-settings.js <Roblox dir> [<placeId>=<projectName> ...] [never place id ...]
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const rest = process.argv.slice(3).map(String);
const seeds = rest.filter((a) => a.includes("=")).map((a) => a.split(/=(.*)/s).slice(0, 2))
  .filter(([id, name]) => /^\d+$/.test(id) && name);
const never = new Set(rest.filter((a) => !a.includes("=")));
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
  // an empty Lua table comes back as [] — the plugin indexes it by place id, so make it an object
  let prior = data.Rojo_priorEndpoints;
  if (!prior || typeof prior !== "object" || Array.isArray(prior)) prior = {};
  for (const k of Object.keys(prior)) if (never.has(k)) delete prior[k];
  for (const [id, name] of seeds) {
    if (never.has(id) || (prior[id] && prior[id].projectName === name)) continue;
    prior[id] = { projectName: name, timestamp: Math.floor(Date.now() / 1000) };
  }
  if (Object.keys(prior).length || data.Rojo_priorEndpoints !== undefined) data.Rojo_priorEndpoints = prior;
  if (JSON.stringify(data) !== before) {
    fs.writeFileSync(f + ".tmp", JSON.stringify(data));
    fs.renameSync(f + ".tmp", f);
    console.log("studio plugin settings set in " + f);
  }
}
