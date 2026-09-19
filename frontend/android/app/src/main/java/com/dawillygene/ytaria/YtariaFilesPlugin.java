package com.dawillygene.ytaria;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import androidx.core.content.FileProvider;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Copies a finished download from the app cache into the public Downloads collection.
 *
 * Android 10+ (API 29+): MediaStore.Downloads - needs NO storage permission.
 * Android 7-9 (API 24-28): falls back to the app-specific external Downloads directory (also no
 * permission), exposed through the FileProvider. Files there are removed if the app is uninstalled.
 *
 * Security: only files inside this app's cache directory may be copied, so a compromised WebView
 * cannot use this plugin to exfiltrate other app-private files into public storage.
 */
@CapacitorPlugin(name = "YtariaFiles")
public class YtariaFilesPlugin extends Plugin {

    @PluginMethod
    public void saveToDownloads(PluginCall call) {
        String rawPath = call.getString("path");
        String displayName = call.getString("displayName");
        String mimeType = call.getString("mimeType", "application/octet-stream");
        if (rawPath == null || displayName == null) {
            call.reject("path and displayName are required");
            return;
        }
        Context ctx = getContext();
        File source;
        try {
            source = resolveCacheFile(ctx, rawPath);
        } catch (IOException | SecurityException e) {
            call.reject("Refusing to save a file outside the app cache");
            return;
        }
        String safeName = sanitize(displayName);
        try {
            Uri result;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                result = saveViaMediaStore(ctx, source, safeName, mimeType);
            } else {
                result = saveToAppExternal(ctx, source, safeName);
            }
            JSObject ret = new JSObject();
            ret.put("uri", result.toString());
            call.resolve(ret);
        } catch (IOException e) {
            call.reject("Could not save the file: " + e.getMessage());
        }
    }

    @PluginMethod
    public void openFile(PluginCall call) {
        String uriString = call.getString("uri");
        String mimeType = call.getString("mimeType", "*/*");
        if (uriString == null) {
            call.reject("uri is required");
            return;
        }
        Uri uri = Uri.parse(uriString);
        String scheme = uri.getScheme();
        if (!"content".equals(scheme)) { // only content:// URIs produced by saveToDownloads
            call.reject("Unsupported uri");
            return;
        }
        Intent view = new Intent(Intent.ACTION_VIEW);
        view.setDataAndType(uri, mimeType);
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            getContext().startActivity(Intent.createChooser(view, null).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            call.resolve();
        } catch (Exception e) {
            call.reject("No app can open this file");
        }
    }

    private static File resolveCacheFile(Context ctx, String rawPath) throws IOException {
        String path = rawPath.startsWith("file://") ? Uri.parse(rawPath).getPath() : rawPath;
        if (path == null) throw new SecurityException("bad path");
        File file = new File(path).getCanonicalFile();
        String cacheRoot = ctx.getCacheDir().getCanonicalPath() + File.separator;
        if (!file.getPath().startsWith(cacheRoot) || !file.isFile()) throw new SecurityException("outside cache");
        return file;
    }

    private static String sanitize(String name) {
        String cleaned = name.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]+", "_").trim();
        if (cleaned.isEmpty() || cleaned.equals(".") || cleaned.equals("..")) cleaned = "download";
        return cleaned.length() > 120 ? cleaned.substring(0, 120) : cleaned;
    }

    private static Uri saveViaMediaStore(Context ctx, File source, String name, String mime) throws IOException {
        ContentResolver resolver = ctx.getContentResolver();
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
        values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/ytaria");
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);
        Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (uri == null) throw new IOException("MediaStore insert failed");
        try (InputStream in = new FileInputStream(source); OutputStream out = resolver.openOutputStream(uri)) {
            if (out == null) throw new IOException("cannot open output stream");
            copy(in, out);
        } catch (IOException e) {
            resolver.delete(uri, null, null); // never leave a half-written entry behind
            throw e;
        }
        ContentValues done = new ContentValues();
        done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        resolver.update(uri, done, null, null);
        return uri;
    }

    private static Uri saveToAppExternal(Context ctx, File source, String name) throws IOException {
        File dir = ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        if (dir == null) throw new IOException("external storage unavailable");
        File dest = new File(dir, name);
        int n = 1;
        while (dest.exists()) dest = new File(dir, "(" + (n++) + ") " + name);
        try (InputStream in = new FileInputStream(source); OutputStream out = new java.io.FileOutputStream(dest)) {
            copy(in, out);
        } catch (IOException e) {
            dest.delete();
            throw e;
        }
        return FileProvider.getUriForFile(ctx, ctx.getPackageName() + ".fileprovider", dest);
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[256 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        out.flush();
    }
}
