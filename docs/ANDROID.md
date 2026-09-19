# Android (Capacitor) app

Package `com.dawillygene.ytaria`, app name `ytaria-manager`, min SDK 24, target/compile SDK 36. Capacitor 8.x. The same React app
as the web UI is bundled into the APK (`frontend/dist` → `android/app/src/main/assets/public`). **iOS is not built or tested;** the shared
code avoids web-only assumptions so an iOS project can be added later (`npx cap add ios` + a Swift equivalent of `YtariaFilesPlugin`).

## Build
Prerequisites: Node 22, JDK 21 (`JAVA_HOME`), Android SDK with platform 36 + build-tools (`ANDROID_HOME`).
```bash
cd frontend && npm ci
# Debug build for an emulator/dev API (cleartext + WebView debugging allowed ONLY in this mode):
echo 'VITE_API_BASE_URL=http://10.0.2.2:8000' > .env.android.local     # 10.0.2.2 = host machine from the emulator
npm run build:android && (cd android && ./gradlew assembleDebug)       # → app/build/outputs/apk/debug/app-debug.apk
# Release build (HTTPS API required; the build FAILS otherwise, and verifies the synced config is release-safe):
echo 'VITE_API_BASE_URL=https://downloads.example.com' > .env.androidrelease.local
npm run build:android:release && (cd android && ./gradlew assembleRelease)
```
The API must list the WebView origin in `YTARIA_CORS_ORIGINS=https://localhost`. Release has **no** `server.url` (no live reload), no cleartext,
no mixed content, no WebView debugging; `scripts/verify-release-config.mjs` enforces this after `cap sync`.

**Signing (not provided):** without keystore variables `assembleRelease` yields `app-release-unsigned.apk`. To sign, export
`YTARIA_KEYSTORE_FILE`, `YTARIA_KEYSTORE_PASSWORD`, `YTARIA_KEY_ALIAS`, `YTARIA_KEY_PASSWORD` (never commit them) or sign the unsigned APK with `apksigner`.
Play Store publication needs your developer account, an app bundle (`./gradlew bundleRelease`) and Play App Signing – none of that was done.
Branding: launcher icon and splash are **placeholders** (`res/drawable-v24/ic_launcher_foreground.xml`, `res/drawable/splash.xml`); replace them.

## What is native
* **Secure session storage**: `@aparajita/capacitor-secure-storage` v8 (AES-GCM with an Android Keystore key, stored in app-private preferences). Only the refresh token is stored; the
  access token is memory-only. Nothing goes to Capacitor Preferences/`localStorage`. `allowBackup=false`.
* **Saving files**: `YtariaFilesPlugin` (Java, `android/app/src/main/java/com/dawillygene/ytaria/`). `@capacitor/filesystem` streams the download into the app cache;
  the plugin copies it to **Downloads/ytaria** via MediaStore (API 29+, no permission) or the app-specific external directory (API 24-28, no permission), then deletes the cache copy.
  It refuses any path outside the app cache, and `openFile` only accepts `content://` URIs. FileProvider paths are limited to app-private locations.
* **Network status** (`@capacitor/network`), **back button** (`@capacitor/app`: closes a dialog → returns to the Downloads tab → exits), **safe areas** (`env(safe-area-inset-*)`, `viewport-fit=cover`).
* Permissions in the built APK: `INTERNET`, `ACCESS_NETWORK_STATE` (plus the app-signature-scoped receiver permission Android adds). No storage, notification or foreground-service permissions.

## Transfer behaviour and limits (read this)
The device transfer is a **foreground** flow: the app must stay open until "Saved on device". `Filesystem.downloadFile` runs natively and streams to disk, but it is not a
background/durable transfer: if Android kills the app, the partial cache file is discarded and the user taps *Try again* (no byte-range resume on device in v1).
Server-side downloads are independent and continue whether or not the app is open; only the last hop (server → phone) needs the app. A durable background transfer would need a
foreground service or WorkManager in native code; it was not built.

## Evidence
Executed on an Android 15 (API 35) x86_64 emulator (headless, KVM), debug APK against a local API: register → sign in (native bearer flow) → inspect a public-domain archive.org video →
queue → completed on server ("Ready on server / Not on your device yet") → *Save to this device* → file present at `/sdcard/Download/ytaria/…ogv`, 26,138,707 bytes, SHA-256 matching the server-side transfer,
UI "Saved on device" → force-stop and relaunch stayed signed in (refresh token restored from secure storage) → *Open* launched the system chooser. A signed (throw-away key) **release** build with R8 minification
launched without errors. Not tested: physical devices, Android <10 fallback path at runtime, Play/production signing, background/kill behaviour, low-storage errors.
That run also found and fixed two real bugs (missing cache directory before download; native errors leaking file paths into the UI).
