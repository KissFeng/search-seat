#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$ROOT_DIR/app"
BUILD_DIR="$ROOT_DIR/build"
DIST_DIR="$ROOT_DIR/dist"

ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
ANDROID_JAR="$ANDROID_HOME/platforms/android-36/android.jar"
BUILD_TOOLS="$ANDROID_HOME/build-tools/36.1.0"

if [[ ! -f "$ANDROID_JAR" ]]; then
  ANDROID_JAR="$(find "$ANDROID_HOME/platforms" -name android.jar | sort | tail -1)"
fi

if [[ ! -d "$BUILD_TOOLS" ]]; then
  BUILD_TOOLS="$(find "$ANDROID_HOME/build-tools" -maxdepth 1 -type d | sort | tail -1)"
fi

AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"

for tool in "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER"; do
  if [[ ! -x "$tool" ]]; then
    echo "Missing Android build tool: $tool" >&2
    exit 1
  fi
done

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/dex" "$DIST_DIR"

"$AAPT2" compile --dir "$APP_DIR/src/main/res" -o "$BUILD_DIR/res.zip"

"$AAPT2" link \
  -I "$ANDROID_JAR" \
  --manifest "$APP_DIR/src/main/AndroidManifest.xml" \
  --java "$BUILD_DIR/gen" \
  --min-sdk-version 23 \
  --target-sdk-version 36 \
  --auto-add-overlay \
  -o "$BUILD_DIR/app-unsigned.apk" \
  "$BUILD_DIR/res.zip"

find "$BUILD_DIR/gen" "$APP_DIR/src/main/java" -name '*.java' | sort > "$BUILD_DIR/sources.txt"

javac \
  -source 8 \
  -target 8 \
  -encoding UTF-8 \
  -classpath "$ANDROID_JAR" \
  -d "$BUILD_DIR/classes" \
  @"$BUILD_DIR/sources.txt"

"$D8" \
  --min-api 23 \
  --lib "$ANDROID_JAR" \
  --output "$BUILD_DIR/dex" \
  $(find "$BUILD_DIR/classes" -name '*.class' | sort)

cp "$BUILD_DIR/app-unsigned.apk" "$BUILD_DIR/app-unsigned-dex.apk"
(cd "$BUILD_DIR/dex" && zip -q -r "$BUILD_DIR/app-unsigned-dex.apk" classes.dex)

"$ZIPALIGN" -f -p 4 "$BUILD_DIR/app-unsigned-dex.apk" "$BUILD_DIR/app-aligned.apk"

KEYSTORE="$ROOT_DIR/debug.keystore"
if [[ ! -f "$KEYSTORE" ]]; then
  keytool -genkeypair \
    -keystore "$KEYSTORE" \
    -storepass android \
    -keypass android \
    -alias androiddebugkey \
    -keyalg RSA \
    -keysize 2048 \
    -validity 10000 \
    -dname "CN=Android Debug,O=Search Seat,C=CN" >/dev/null
fi

APK="$DIST_DIR/search-seat-webview.apk"
"$APKSIGNER" sign \
  --ks "$KEYSTORE" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$APK" \
  "$BUILD_DIR/app-aligned.apk"

"$APKSIGNER" verify --verbose "$APK"
echo "Built $APK"
