"""
PWA Asset & Manifest Generator for Monika AI & Jarvis Core.
Generates responsive PWA icons, maskable icons, apple-touch-icon, favicons, and manifest.json.
"""

import sys
import json
import shutil
from pathlib import Path
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"
ICONS_DIR = WEBAPP_DIR / "icons"
AVATAR_PATH = WEBAPP_DIR / "monika_avatar.jpg"
BG_COLOR = (11, 15, 25, 255)  # #0B0F19

def generate_icons():
    """Generate all required PWA icons from monika_avatar.jpg."""
    print(f"Ensuring icons directory exists at: {ICONS_DIR}")
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    if not AVATAR_PATH.exists():
        raise FileNotFoundError(f"Avatar file not found: {AVATAR_PATH}")

    print(f"Opening avatar source: {AVATAR_PATH}")
    with Image.open(AVATAR_PATH) as img:
        # Convert source to RGBA
        avatar_rgba = img.convert("RGBA")

        # 1. Standard Icons (any)
        # 192x192
        icon_192 = avatar_rgba.resize((192, 192), Image.Resampling.LANCZOS)
        icon_192_path = ICONS_DIR / "icon-192x192.png"
        icon_192.save(icon_192_path, format="PNG", optimize=True)
        print(f"Generated: {icon_192_path} ({icon_192_path.stat().st_size} bytes)")

        # 512x512
        icon_512 = avatar_rgba.resize((512, 512), Image.Resampling.LANCZOS)
        icon_512_path = ICONS_DIR / "icon-512x512.png"
        icon_512.save(icon_512_path, format="PNG", optimize=True)
        print(f"Generated: {icon_512_path} ({icon_512_path.stat().st_size} bytes)")

        # 2. Maskable Icons (safe area with 80% inner content centered on #0B0F19 background)
        # maskable-192x192 (safe area diameter ~80%, inner size 154x154, margin 19px)
        maskable_192 = Image.new("RGBA", (192, 192), BG_COLOR)
        inner_192_size = int(192 * 0.8)  # 153px, let's use 154px for even margin
        if inner_192_size % 2 != 0:
            inner_192_size += 1
        inner_192 = avatar_rgba.resize((inner_192_size, inner_192_size), Image.Resampling.LANCZOS)
        offset_192 = ((192 - inner_192_size) // 2, (192 - inner_192_size) // 2)
        maskable_192.paste(inner_192, offset_192, inner_192)
        maskable_192_path = ICONS_DIR / "icon-maskable-192x192.png"
        maskable_192.save(maskable_192_path, format="PNG", optimize=True)
        print(f"Generated: {maskable_192_path} ({maskable_192_path.stat().st_size} bytes)")

        # maskable-512x512 (inner size ~410x410, margin 51px)
        maskable_512 = Image.new("RGBA", (512, 512), BG_COLOR)
        inner_512_size = int(512 * 0.8)  # 409 -> 410px for even margin
        if inner_512_size % 2 != 0:
            inner_512_size += 1
        inner_512 = avatar_rgba.resize((inner_512_size, inner_512_size), Image.Resampling.LANCZOS)
        offset_512 = ((512 - inner_512_size) // 2, (512 - inner_512_size) // 2)
        maskable_512.paste(inner_512, offset_512, inner_512)
        maskable_512_path = ICONS_DIR / "icon-maskable-512x512.png"
        maskable_512.save(maskable_512_path, format="PNG", optimize=True)
        print(f"Generated: {maskable_512_path} ({maskable_512_path.stat().st_size} bytes)")

        # 3. Apple Touch Icon (180x180, standard iOS icon)
        apple_icon = avatar_rgba.resize((180, 180), Image.Resampling.LANCZOS)
        apple_icon_path = ICONS_DIR / "apple-touch-icon.png"
        apple_icon.save(apple_icon_path, format="PNG", optimize=True)
        print(f"Generated: {apple_icon_path} ({apple_icon_path.stat().st_size} bytes)")

        # 4. Multi-resolution favicon.ico (16x16, 32x32, 48x48)
        favicon_icons_path = ICONS_DIR / "favicon.ico"
        favicon_root_path = WEBAPP_DIR / "favicon.ico"
        
        # Save multi-size ICO
        avatar_rgba.save(
            favicon_icons_path,
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48)]
        )
        print(f"Generated: {favicon_icons_path} ({favicon_icons_path.stat().st_size} bytes)")

        # Copy to webapp root favicon.ico
        shutil.copy2(favicon_icons_path, favicon_root_path)
        print(f"Copied to root: {favicon_root_path} ({favicon_root_path.stat().st_size} bytes)")

def generate_manifest():
    """Create production-grade manifest.json with full PWA parameters."""
    manifest_data = {
        "id": "/?source=pwa",
        "name": "Monika AI & Jarvis Core",
        "short_name": "Monika",
        "description": "Monika AI yordamchisi va Jarvis shaxsiy kompyuter boshqaruv tizimi",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#0B0F19",
        "theme_color": "#0B0F19",
        "lang": "uz",
        "dir": "ltr",
        "categories": ["utilities", "productivity"],
        "icons": [
            {
                "src": "/icons/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/icons/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/icons/icon-maskable-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable"
            },
            {
                "src": "/icons/icon-maskable-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            },
            {
                "src": "/icons/apple-touch-icon.png",
                "sizes": "180x180",
                "type": "image/png"
            }
        ],
        "shortcuts": [
            {
                "name": "Kamera & Vision",
                "short_name": "Vision",
                "description": "Monika kompyuter ko'rish va ish stoli nazorati",
                "url": "/#vision",
                "icons": [
                    {
                        "src": "/icons/icon-192x192.png",
                        "sizes": "192x192",
                        "type": "image/png"
                    }
                ]
            },
            {
                "name": "Monika Chat",
                "short_name": "Chat",
                "description": "Monika AI bilan to'g'ridan-to'g'ri suhbat",
                "url": "/#chat",
                "icons": [
                    {
                        "src": "/icons/icon-192x192.png",
                        "sizes": "192x192",
                        "type": "image/png"
                    }
                ]
            }
        ]
    }

    manifest_path = WEBAPP_DIR / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)
    print(f"Generated manifest: {manifest_path} ({manifest_path.stat().st_size} bytes)")

def verify_assets():
    """Verify all generated files exist, are valid images/JSON, and non-empty."""
    required_files = [
        ICONS_DIR / "icon-192x192.png",
        ICONS_DIR / "icon-512x512.png",
        ICONS_DIR / "icon-maskable-192x192.png",
        ICONS_DIR / "icon-maskable-512x512.png",
        ICONS_DIR / "apple-touch-icon.png",
        ICONS_DIR / "favicon.ico",
        WEBAPP_DIR / "favicon.ico",
        WEBAPP_DIR / "manifest.json",
    ]

    print("\n=== VERIFICATION RESULTS ===")
    all_ok = True
    for p in required_files:
        if not p.exists():
            print(f"❌ MISSING: {p}")
            all_ok = False
            continue
        size = p.stat().st_size
        if size == 0:
            print(f"❌ EMPTY: {p}")
            all_ok = False
            continue

        if p.suffix.lower() == ".png":
            try:
                with Image.open(p) as test_img:
                    test_img.verify()
                with Image.open(p) as test_img:
                    test_img.load()
                    print(f"✅ {p.name}: {size} bytes | Dim: {test_img.size} | Mode: {test_img.mode}")
            except Exception as e:
                print(f"❌ CORRUPTED PNG {p.name}: {e}")
                all_ok = False
        elif p.suffix.lower() == ".ico":
            try:
                with Image.open(p) as test_img:
                    test_img.verify()
                with Image.open(p) as test_img:
                    test_img.load()
                    print(f"✅ {p.name}: {size} bytes | Dim: {test_img.size} | Format: {test_img.format}")
            except Exception as e:
                print(f"❌ CORRUPTED ICO {p.name}: {e}")
                all_ok = False
        elif p.suffix.lower() == ".json":
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    icons_count = len(data.get("icons", []))
                    shortcuts_count = len(data.get("shortcuts", []))
                    print(f"✅ {p.name}: {size} bytes | Valid JSON | Icons: {icons_count} | Shortcuts: {shortcuts_count}")
            except Exception as e:
                print(f"❌ INVALID JSON {p.name}: {e}")
                all_ok = False

    if all_ok:
        print("\n🎉 ALL ASSETS & MANIFEST VERIFIED SUCCESSFULLY!")
    else:
        print("\n⚠️ SOME ASSETS FAILED VERIFICATION.")
        sys.exit(1)

if __name__ == "__main__":
    generate_icons()
    generate_manifest()
    verify_assets()
