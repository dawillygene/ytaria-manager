// Fails the release build if the synced Android config is not release-safe.
import { readFileSync } from "node:fs";

const cfg = JSON.parse(readFileSync("android/app/src/main/assets/capacitor.config.json", "utf8"));
const problems = [];
if (cfg.server?.url) problems.push("server.url (live reload) must not be set in a release build");
if (cfg.server?.cleartext) problems.push("server.cleartext must be false");
if (cfg.android?.allowMixedContent) problems.push("android.allowMixedContent must be false");
if (cfg.android?.webContentsDebuggingEnabled) problems.push("webContentsDebuggingEnabled must be false");
const bundle = readFileSync("android/app/src/main/assets/public/index.html", "utf8");
if (/10\.0\.2\.2|localhost:5173/.test(bundle)) problems.push("bundle references a dev host");
if (problems.length) {
  console.error("Release config check FAILED:\n - " + problems.join("\n - "));
  process.exit(1);
}
console.log("Release config check passed.");
