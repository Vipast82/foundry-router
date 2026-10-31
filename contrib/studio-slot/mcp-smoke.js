// Studio slot smoke test: drive Studio through the community robloxstudio-mcp server,
// the same MCP the Foundry gate runner uses. Prints each step's result.
//   node mcp-smoke.js            (inside the container, Studio open on a place)
const { spawn } = require("child_process");
// robloxstudio-mcp is installed in the image; npx falls back to the registry elsewhere

// MCP_CMD='["wine", ".../StudioMCP.exe"]': test another MCP server (Roblox's official one)
const cmd = process.env.MCP_CMD ? JSON.parse(process.env.MCP_CMD) : ["npx", "-y", "@chrrxs/robloxstudio-mcp@3.4.2"];
const srv = spawn(cmd[0], cmd.slice(1), {
  stdio: ["pipe", "pipe", "inherit"],
});
let buf = "";
const waiting = new Map();
let id = 0;
srv.stdout.on("data", (d) => {
  buf += d.toString();
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); }
  }
});
function call(method, params, timeoutMs = 60000) {
  const n = ++id;
  srv.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: n, method, params }) + "\n");
  return new Promise((res, rej) => {
    waiting.set(n, res);
    setTimeout(() => rej(new Error(`${method} timed out`)), timeoutMs);
  });
}
const text = (r) => JSON.stringify(r.result ?? r.error).slice(0, 600);

(async () => {
  const init = await call("initialize", {
    protocolVersion: "2025-06-18", capabilities: {},
    clientInfo: { name: "studio-slot-smoke", version: "1" },
  });
  console.log("initialize:", init.result?.serverInfo?.name, init.result?.serverInfo?.version);
  srv.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  // the Studio plugin polls the server; give it time to attach
  await new Promise((r) => setTimeout(r, Number(process.env.WAIT_MS || 8000)));
  const steps = [
    ["get_connected_instances", {}],
    ["get_place_info", {}],
    ["execute_luau", { code: "return game.Name .. ' | services: ' .. #game:GetChildren() .. ' | ServerScriptService children: ' .. #game:GetService('ServerScriptService'):GetChildren()" }],
  ];
  // PLAYTEST=1: also check the Rojo sync landed, then run a short solo playtest
  if (process.env.PLAYTEST) steps.push(
    ["execute_luau", { code: "local rs = game:GetService('ReplicatedStorage'); local a = rs:FindFirstChild('Assets'); local e = a and a:FindFirstChild('Enemies'); return 'Rojo sync: Assets=' .. tostring(a ~= nil) .. ' Enemies=' .. (e and #e:GetChildren() or 0)" }],
    ["solo_playtest", { action: "start", mode: "run", timeout: 90 }],
    ["eval_server_runtime", { code: "task.wait(5); return 'server up | players=' .. #game:GetService('Players'):GetPlayers() .. ' | workspace children=' .. #workspace:GetChildren() .. ' | time=' .. string.format('%.1f', workspace.DistributedGameTime)" }],
    ["get_runtime_logs", { tail: 15 }],
    ["solo_playtest", { action: "stop" }],
  );
  // STEPS='[["tool", {args}], ...]': run these instead (images saved to OUT_DIR, default /tmp)
  if (process.env.STEPS) steps.splice(0, steps.length, ...JSON.parse(process.env.STEPS));
  let shot = 0;
  for (const [name, args] of steps) {
    try {
      const r = await call("tools/call", { name, arguments: args }, 180000);
      for (const c of r.result?.content || []) {
        if (c.type !== "image") continue;
        const ext = (c.mimeType || "image/png").split("/")[1].replace("jpeg", "jpg");
        const f = `${process.env.OUT_DIR || "/tmp"}/mcp-shot-${++shot}.${ext}`;
        require("fs").writeFileSync(f, Buffer.from(c.data, "base64"));
        console.log(`${name}: image saved ${f}`);
      }
      const t = (r.result?.content || []).filter((c) => c.type !== "image");
      const out = JSON.stringify(t.length ? t : r.error ?? r.result?.structuredContent ?? "");
      console.log(`${name}:`, process.env.FULL ? out : out.slice(0, 600));
    } catch (e) { console.log(`${name}: ERROR ${e.message}`); }
  }
  srv.kill();
  process.exit(0);
})().catch((e) => { console.log("FAILED", e.message); srv.kill(); process.exit(1); });
