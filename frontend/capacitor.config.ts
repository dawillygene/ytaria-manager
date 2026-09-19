import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Production config: the bundled assets in `dist/` are what the app runs. There is intentionally NO
 * `server.url` (live reload) here; never add one to a release build.
 * The WebView origin is https://localhost, which must be listed in the API's YTARIA_CORS_ORIGINS.
 */
// CAP_DEV=1 is set ONLY by `npm run build:android` (emulator/dev). It allows the WebView to call a cleartext
// dev API such as http://10.0.2.2:8000 and enables remote debugging. Release builds never set it.
const dev = process.env.CAP_DEV === "1";

const config: CapacitorConfig = {
  appId: "com.dawillygene.ytaria",
  appName: "ytaria-manager",
  webDir: "dist",
  server: { androidScheme: "https" },
  android: { allowMixedContent: dev, webContentsDebuggingEnabled: dev },
  plugins: {
    SplashScreen: { launchShowDuration: 800, backgroundColor: "#0f766e", showSpinner: false },
  },
};

export default config;
