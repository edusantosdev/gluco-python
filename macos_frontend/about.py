"""The About window: the standard panel, with credits listing the project, license and open source.

Laid out like Cushion's About window (edusantosdev/cushion-python ``about.py``) so the apps match.
"""

from __future__ import annotations

from AppKit import (
    NSApplication,
    NSAttributedString,
    NSCenterTextAlignment,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImage,
    NSLinkAttributeName,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSWorkspace,
)
from Foundation import NSURL, NSBundle, NSSize

from glucose_common.version import package_version

APP_NAME = "Gluco"
BUNDLE_ID = "com.glucobar.frontend"  # see scripts/build_portable_app.sh and Info.plist.tmpl
AUTHOR = "Eduardo Santos"
REPO_URL = "https://github.com/edusantosdev/gluco-python"
COPYRIGHT = f"Copyright © 2026 {AUTHOR}"

TAGLINE = (
    f"{APP_NAME} keeps your latest LibreView glucose reading and trend in the menu bar, so you "
    "can see whether you’re in range, rising or falling without picking up your phone."
)
# Keep in step with pyproject.toml, NOTICE and ATTRIBUTIONS.md.
OPEN_SOURCE = [
    ("Python", "https://www.python.org"),
    ("PyObjC", "https://github.com/ronaldoussoren/pyobjc"),
    ("rumps", "https://github.com/jaredks/rumps"),
    ("pylibrelinkup", "https://pypi.org/project/pylibrelinkup/"),
    ("FastAPI", "https://fastapi.tiangolo.com"),
    ("Uvicorn", "https://www.uvicorn.org"),
    ("Pydantic", "https://docs.pydantic.dev"),
    ("pydantic-settings", "https://github.com/pydantic/pydantic-settings"),
    ("HTTPX", "https://www.python-httpx.org"),
    ("python-dotenv", "https://github.com/theskumar/python-dotenv"),
    ("keyring", "https://github.com/jaraco/keyring"),
    ("py2app", "https://github.com/ronaldoussoren/py2app"),
]
DISCLAIMER = (
    f"{APP_NAME} isn’t a medical device. Don’t use it for diagnosis, treatment, dosing or any "
    "other medical decision: readings may be delayed, missing or wrong, so always check your "
    "LibreLink app, glucose meter or healthcare professional.\n\n"
    f"{APP_NAME} is an independent open-source project. It isn’t affiliated with, endorsed by or "
    "supported by Abbott. It reads glucose data through unofficial LibreView APIs that may change "
    "or stop working at any time. FreeStyle Libre, LibreLink, LibreLinkUp and LibreView are "
    "trademarks of Abbott."
)

# ``NSWorkspace.iconForFile_`` often returns a 32pt logical size; the panel sizes the
# header artwork from ``NSImage.size``, so scale it up. Never downscale a large icon.
_ICON_EDGE_PT = 192.0


def _credits() -> NSAttributedString:
    size = NSFont.smallSystemFontSize()
    paragraph = NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(NSCenterTextAlignment)
    text = NSMutableAttributedString.alloc().init()

    def append(string, *, heading=False, link=None, color=None):
        font = (
            NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold)
            if heading
            else NSFont.systemFontOfSize_(size)
        )
        attributes = {
            NSFontAttributeName: font,
            # Set explicitly: credits without a color draw black, even in dark mode.
            NSForegroundColorAttributeName: color or NSColor.labelColor(),
            NSParagraphStyleAttributeName: paragraph,
        }
        if link:
            attributes[NSLinkAttributeName] = NSURL.URLWithString_(link)
        piece = NSAttributedString.alloc().initWithString_attributes_(string, attributes)
        text.appendAttributedString_(piece)

    append(f"{TAGLINE}\n\n")
    append("Created by\n", heading=True)
    append(f"{AUTHOR}\n\n")
    append("Source and issues\n", heading=True)
    append(REPO_URL, link=REPO_URL)
    append("\n\nLicense\n", heading=True)
    append(f"Apache License 2.0. {APP_NAME} is provided as is, without warranties of any kind.\n\n")
    append("Open source\n", heading=True)
    for i, (name, url) in enumerate(OPEN_SOURCE):
        append(" · " if i else "")
        append(name, link=url)
    append("\nand the libraries they use, listed with their licenses in ATTRIBUTIONS.md.\n\n")
    append(DISCLAIMER, color=NSColor.secondaryLabelColor())
    return text


def _version() -> str:
    """Prefer the bundle's ``Info.plist`` so the panel matches the shipped app.

    Run outside ``Gluco.app``, the main bundle is Python's, whose version isn't ours.
    """
    bundle = NSBundle.mainBundle()
    if bundle is not None and bundle.bundleIdentifier() == BUNDLE_ID:
        version = bundle.objectForInfoDictionaryKey_("CFBundleShortVersionString")
        if version:
            return str(version)
    return package_version()


def _icon():
    """The icon Finder shows for this ``.app``.

    An ``LSUIElement`` app can get a blank icon in the panel unless one is passed, and
    loading ``Gluco.icns`` directly can pick a different representation than Finder does.
    """
    bundle = NSBundle.mainBundle()
    app_path = bundle.bundlePath() if bundle is not None else None
    if not app_path:
        return None
    icon = NSWorkspace.sharedWorkspace().iconForFile_(app_path)
    if icon is None:
        path = bundle.pathForResource_ofType_(APP_NAME, "icns")
        icon = NSImage.alloc().initWithContentsOfFile_(path) if path else None
    if icon is not None:
        s = icon.size()
        edge = max(s.width, s.height)
        if 0 < edge < _ICON_EDGE_PT:
            k = _ICON_EDGE_PT / edge
            icon.setSize_(NSSize(s.width * k, s.height * k))
    return icon


def show_about() -> None:
    app = NSApplication.sharedApplication()
    # A menubar-only app must come forward for the panel to show.
    app.activateIgnoringOtherApps_(True)
    # Without "Version", the panel fills in Python's own build number.
    options = {
        "ApplicationName": APP_NAME,
        "ApplicationVersion": _version(),
        "Version": "",
        "Copyright": COPYRIGHT,
        "Credits": _credits(),
    }
    icon = _icon()
    if icon is not None:
        options["ApplicationIcon"] = icon
    app.orderFrontStandardAboutPanelWithOptions_(options)
