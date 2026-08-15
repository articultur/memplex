#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const memplexBin = require.resolve("memplex/bin/memplex.js");
const result = spawnSync(process.execPath, [memplexBin, "setup"].concat(process.argv.slice(2)), {
  stdio: "inherit",
  env: process.env,
});

process.exit(result.status === null ? 1 : result.status);
