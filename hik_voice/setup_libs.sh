#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_libs.sh  –  Prepare Hikvision arm64 native libraries on Raspberry Pi
#
# Usage (run on the Pi):
#   chmod +x setup_libs.sh
#   ./setup_libs.sh /path/to/AccessControl.apk
#
# What it does:
#   1. Extracts arm64-v8a .so files from the APK (which is a ZIP)
#   2. Creates stub libandroid.so + liblog.so (only symbols used by audio path)
#   3. Strips Android @LIBC symbol-version tags so glibc can resolve them
#   4. Tests that libezstreamclient.so loads cleanly in Python
#
# Output: ~/hik_voice_libs/
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APK="${1:?Usage: $0 /path/to/AccessControl.apk}"
LIB_DIR="$HOME/hik_voice_libs"

echo "==> Creating $LIB_DIR"
mkdir -p "$LIB_DIR"

# ── 1. Extract arm64 libs from APK ───────────────────────────────────────────
echo "==> Extracting arm64-v8a libs from APK…"
unzip -jo "$APK" "lib/arm64-v8a/*.so" -d "$LIB_DIR"
echo "    $(ls "$LIB_DIR"/*.so | wc -l) libraries extracted"

# ── 2. Stub libandroid.so ─────────────────────────────────────────────────────
# Only one symbol from libandroid is referenced: ANativeWindow_fromSurface
# It is used for VIDEO rendering only — never called on the audio path.
echo "==> Building stub libandroid.so…"
cat > /tmp/android_stub.c << 'EOF'
// Stub: ANativeWindow_fromSurface is video-only; return NULL safely.
void* ANativeWindow_fromSurface(void* env, void* surface) { return (void*)0; }
void  ANativeWindow_release(void* window) {}
int   ANativeWindow_lock(void* w, void* bi, void* fence) { return -1; }
int   ANativeWindow_unlockAndPost(void* w) { return -1; }
EOF
gcc -shared -fPIC -o "$LIB_DIR/libandroid.so" /tmp/android_stub.c
echo "    libandroid.so created"

# ── 3. Stub liblog.so ─────────────────────────────────────────────────────────
echo "==> Building stub liblog.so…"
cat > /tmp/log_stub.c << 'EOF'
#include <stdarg.h>
int __android_log_write(int prio, const char* tag, const char* msg) { return 0; }
int __android_log_print(int prio, const char* tag, const char* fmt, ...) { return 0; }
int __android_log_vprint(int prio, const char* tag, const char* fmt, va_list ap) { return 0; }
void __android_log_assert(const char* cond, const char* tag, const char* fmt, ...) {}
EOF
gcc -shared -fPIC -o "$LIB_DIR/liblog.so" /tmp/log_stub.c
echo "    liblog.so created"

# ── 4. Strip @LIBC version tags ───────────────────────────────────────────────
# Android bionic exports pthread_create@LIBC; glibc exports pthread_create@@GLIBC_2.xx
# patchelf --clear-symbol-version removes the version requirement so glibc satisfies it.
echo "==> Stripping @LIBC version tags (requires patchelf)…"
command -v patchelf >/dev/null 2>&1 || { echo "ERROR: patchelf not found. Run: sudo apt install patchelf"; exit 1; }

patched=0
for lib in "$LIB_DIR"/*.so; do
    # Collect all symbols that have @LIBC version tags
    syms=$(nm -D "$lib" 2>/dev/null \
           | awk '/U /{print $NF}' \
           | grep '@LIBC' \
           | sed 's/@.*//' \
           | sort -u)
    if [[ -n "$syms" ]]; then
        for sym in $syms; do
            patchelf --clear-symbol-version "$sym" "$lib" 2>/dev/null || true
        done
        patched=$((patched + 1))
    fi
done
echo "    Patched $patched libraries"

# ── 5. Verify Python can load the main library ────────────────────────────────
echo "==> Testing Python load…"
LD_LIBRARY_PATH="$LIB_DIR" python3 - <<PYEOF
import ctypes, os, sys

lib_dir = os.path.expanduser("~/hik_voice_libs")
try:
    lib = ctypes.CDLL(f"{lib_dir}/libezstreamclient.so")
    print("  ✅ libezstreamclient.so loaded")
    for fn in ("CASClient_InitLib", "CASClient_VoiceTalkStartEx",
               "CASClient_VoiceTalkInputDataEx", "CASClient_VoiceTalkStop",
               "CASClient_GetDevOperationCodeEx"):
        sym = getattr(lib, fn, None)
        print(f"  {'✅' if sym else '❌'} {fn}")
except Exception as e:
    print(f"  ❌ Load failed: {e}")
    sys.exit(1)
PYEOF

echo ""
echo "==> Done.  Libraries in: $LIB_DIR"
echo "    Run: python3 hik_voice/hik_voice.py  (from project root)"
