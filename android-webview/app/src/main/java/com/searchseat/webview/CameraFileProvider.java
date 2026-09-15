package com.searchseat.webview;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

public class CameraFileProvider extends ContentProvider {
    public static final String AUTHORITY = "com.searchseat.webview.camerafileprovider";

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        String name = uri.getLastPathSegment();
        if (name != null) {
            if (name.endsWith(".jpg") || name.endsWith(".jpeg")) {
                return "image/jpeg";
            }
            if (name.endsWith(".png")) {
                return "image/png";
            }
            if (name.endsWith(".webp")) {
                return "image/webp";
            }
        }
        return "image/jpeg";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = fileForUri(uri);
        int fileMode = ParcelFileDescriptor.MODE_READ_ONLY;
        if (mode != null) {
            if (mode.contains("rw")) {
                fileMode = ParcelFileDescriptor.MODE_READ_WRITE | ParcelFileDescriptor.MODE_CREATE;
            } else if (mode.contains("w")) {
                fileMode = ParcelFileDescriptor.MODE_WRITE_ONLY | ParcelFileDescriptor.MODE_CREATE;
            }
        }
        return ParcelFileDescriptor.open(file, fileMode);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File file = fileForUri(uri);
        MatrixCursor cursor = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        cursor.addRow(new Object[]{file.getName(), file.length()});
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("Insert not supported");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        File file = fileForUri(uri);
        if (file.exists() && file.delete()) {
            return 1;
        }
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("Update not supported");
    }

    private File fileForUri(Uri uri) {
        String name = uri.getLastPathSegment();
        if (name == null || name.contains("/") || name.contains("..")) {
            name = "camera_capture.jpg";
        }
        return new File(getContext().getCacheDir(), name);
    }
}
