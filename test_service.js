"use strict";

const { spawnSync } = require("node:child_process");

const suites = [
  ["-m", "unittest", "service_contract"],
  ["-m", "unittest", "discover", "-s", "rotation", "-t", ".", "-v"],
];

for (const args of suites) {
  const result = spawnSync("python3", args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
