#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const scriptUrl =
  process.env.MEMPLEX_INSTALL_SCRIPT_URL ||
  "https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh";

const quotedArgs = ["--agent", "hermes"]
  .concat(process.argv.slice(2))
  .map((arg) => `'${arg.replace(/'/g, `'\\''`)}'`)
  .join(" ");

const command = `curl -fsSL '${scriptUrl}' | bash -s -- ${quotedArgs}`;
const result = spawnSync("bash", ["-lc", command], {
  stdio: "inherit",
  env: process.env,
});

process.exit(result.status === null ? 1 : result.status);
