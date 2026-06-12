#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const scriptUrl =
  process.env.MEMPLEX_INSTALL_SCRIPT_URL ||
  "https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh";
const pythonPackage = process.env.MEMPLEX_PACKAGE || "memplex==3.2.7";

function quote(arg) {
  return `'${String(arg).replace(/'/g, `'\\''`)}'`;
}

function printHelp() {
  console.log(`Memplex agent memory setup

Usage:
  npx memplex setup [options]
  npx memplex install [options]
  npx memplex uninstall [options]

Common options:
  --agent <name>          auto, codex, claude-code, openclaw, hermes, or all
  --project-path <path>   Project path for memory isolation
  --user-id <id>          User id for memory namespace
  --dry-run               Show planned commands/files

Examples:
  npx memplex setup
  npx memplex setup --agent codex --project-path "$PWD"
  npx memplex uninstall --agent all
`);
}

function runInstaller(args) {
  const quotedArgs = ["--package", pythonPackage]
    .concat(args)
    .map(quote)
    .join(" ");
  const command = `curl -fsSL ${quote(scriptUrl)} | bash -s -- ${quotedArgs}`;
  const result = spawnSync("bash", ["-lc", command], {
    stdio: "inherit",
    env: process.env,
  });
  process.exit(result.status === null ? 1 : result.status);
}

const argv = process.argv.slice(2);
const command = argv[0];

if (!command || command === "-h" || command === "--help" || command === "help") {
  printHelp();
  process.exit(0);
}

if (command.startsWith("-")) {
  runInstaller(argv);
}

if (["setup", "install", "stepup"].includes(command)) {
  runInstaller(argv.slice(1));
}

if (["uninstall", "remove"].includes(command)) {
  runInstaller(argv.slice(1).concat("--uninstall"));
}

if (command === "agent" && ["install", "setup"].includes(argv[1])) {
  runInstaller(argv.slice(2));
}

if (command === "agent" && ["uninstall", "remove"].includes(argv[1])) {
  runInstaller(argv.slice(2).concat("--uninstall"));
}

console.error(`Unknown command: ${command}`);
printHelp();
process.exit(1);
