#!/usr/bin/env node
"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");

function candidates() {
  const values = [];
  if (process.env.CLODEX_PYTHON) values.push({ cmd: process.env.CLODEX_PYTHON, prefix: [] });
  if (process.env.PYTHON) values.push({ cmd: process.env.PYTHON, prefix: [] });
  values.push({ cmd: "python3", prefix: [] });
  values.push({ cmd: "python", prefix: [] });
  if (process.platform === "win32") {
    values.push({ cmd: "py", prefix: ["-3.13"] });
    values.push({ cmd: "py", prefix: ["-3.12"] });
  }
  return values;
}

function envWithPythonPath() {
  const env = { ...process.env };
  env.PYTHONPATH = packageRoot + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : "");
  env.CLODEX_NPM_LAUNCHER = __filename;
  return env;
}

function supportsPython312(candidate) {
  const result = spawnSync(
    candidate.cmd,
    [
      ...candidate.prefix,
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)",
    ],
    { env: envWithPythonPath(), stdio: "ignore" },
  );
  return result.status === 0;
}

function findPython() {
  for (const candidate of candidates()) {
    if (supportsPython312(candidate)) return candidate;
  }
  return null;
}

function main(extraArgs = []) {
  const python = findPython();
  if (!python) {
    console.error("clodex requires Python 3.12 or newer. Set CLODEX_PYTHON to a compatible interpreter.");
    return 1;
  }
  const result = spawnSync(
    python.cmd,
    [...python.prefix, "-m", "clodex.npm_bridge", ...extraArgs, ...process.argv.slice(2)],
    { env: envWithPythonPath(), stdio: "inherit" },
  );
  if (result.error) {
    console.error(result.error.message);
    return 1;
  }
  return result.status ?? 1;
}

if (require.main === module) {
  process.exitCode = main();
}

module.exports = { main };
