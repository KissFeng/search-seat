#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${JAVA_HOME:-}" ]] && command -v /usr/libexec/java_home >/dev/null 2>&1; then
  DETECTED_JAVA="$(/usr/libexec/java_home -v 17 2>/dev/null || /usr/libexec/java_home -v 11 2>/dev/null || true)"
  if [[ -n "$DETECTED_JAVA" ]]; then
    export JAVA_HOME="$DETECTED_JAVA"
    export PATH="$JAVA_HOME/bin:$PATH"
  fi
fi

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$ROOT_DIR/app"
BUILD_DIR="$ROOT_DIR/build"
DIST_DIR="$ROOT_DIR/dist"
DEPS_DIR="$ROOT_DIR/.deps"

GETUI_APP_ID="${GETUI_APP_ID:-e03fqc6c5XAQYdkduOD0HA}"
GETUI_GTSDK_VERSION="${GETUI_GTSDK_VERSION:-3.3.15.0}"
GETUI_GTC_VERSION="${GETUI_GTC_VERSION:-3.3.3.0}"
GETUI_MAVEN_BASE="${GETUI_MAVEN_BASE:-https://mvn.getui.com/nexus/content/repositories/releases}"

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
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/dex" "$BUILD_DIR/deps" "$DIST_DIR" "$DEPS_DIR"

download_aar() {
  local group_path="$1"
  local artifact="$2"
  local version="$3"
  local target="$DEPS_DIR/$artifact-$version.aar"
  if [[ ! -f "$target" ]]; then
    curl -fL "$GETUI_MAVEN_BASE/$group_path/$artifact/$version/$artifact-$version.aar" -o "$target"
  fi
  printf '%s\n' "$target"
}

extract_aar_classes() {
  local aar="$1"
  local name="$2"
  local out_dir="$BUILD_DIR/deps/$name"
  mkdir -p "$out_dir"
  unzip -qo "$aar" classes.jar -d "$out_dir"
  mv "$out_dir/classes.jar" "$BUILD_DIR/deps/$name.jar"
}

GTSDK_AAR="$(download_aar "com/getui" "gtsdk" "$GETUI_GTSDK_VERSION")"
GTC_AAR="$(download_aar "com/getui" "gtc" "$GETUI_GTC_VERSION")"
extract_aar_classes "$GTSDK_AAR" "gtsdk"
extract_aar_classes "$GTC_AAR" "gtc"
GETUI_CLASSPATH="$BUILD_DIR/deps/gtsdk.jar:$BUILD_DIR/deps/gtc.jar"

sed "s/__GETUI_APPID__/$GETUI_APP_ID/g" \
  "$APP_DIR/src/main/AndroidManifest.xml" > "$BUILD_DIR/AndroidManifest.xml"

"$AAPT2" compile --dir "$APP_DIR/src/main/res" -o "$BUILD_DIR/res.zip"

"$AAPT2" link \
  -I "$ANDROID_JAR" \
  --manifest "$BUILD_DIR/AndroidManifest.xml" \
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
  -classpath "$ANDROID_JAR:$GETUI_CLASSPATH" \
  -d "$BUILD_DIR/classes" \
  @"$BUILD_DIR/sources.txt"

"$D8" \
  --min-api 23 \
  --lib "$ANDROID_JAR" \
  --output "$BUILD_DIR/dex" \
  $(find "$BUILD_DIR/classes" -name '*.class' | sort) \
  "$BUILD_DIR/deps/gtsdk.jar" \
  "$BUILD_DIR/deps/gtc.jar"

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
