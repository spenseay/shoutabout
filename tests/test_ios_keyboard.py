"""The ShoutAbout keyboard (iOS keyboard extension, ios/ShoutAboutKeyboard): its XcodeGen target,
Info.plist keys, privacy manifest, the app's URL route, and that the few shared Swift files it
compiles don't lean on app-only code.

Swift can't be compiled here (no Mac); these pin down what would otherwise only fail on
Codemagic: a keyboard target that pulls in a type it doesn't compile, or the wrong plist keys."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
IOS = REPO / "ios"


@pytest.fixture(scope="module")
def project():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((IOS / "project.yml").read_text("utf-8"))


@pytest.fixture(scope="module")
def keyboard(project):
    return project["targets"]["ShoutAboutKeyboard"]


def keyboard_swift_files(keyboard) -> list[Path]:
    files: list[Path] = []
    for source in keyboard["sources"]:
        path = IOS / source["path"]
        if path.is_dir():
            files.extend(sorted(path.rglob("*.swift")))
        elif path.suffix == ".swift":
            files.append(path)
    return files


def code_only(text: str) -> str:
    """Swift without string literals and comments (good enough for finding type names)."""
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


DECLARATION = re.compile(r"\b(?:struct|enum|class|protocol|typealias|actor)\s+([A-Z]\w*)")
# Top-level only: a nested type (AudioRecorder.Permission, ComputerFinder.State) is used
# qualified from outside its type, and then its outer name is what has to be compiled.
TOP_LEVEL_DECLARATION = re.compile(
    r"^(?:@\w+(?:\([^)\n]*\))?\s+)*(?:(?:public|internal|private|fileprivate|final|open)\s+)*"
    r"(?:struct|enum|class|protocol|typealias|actor)\s+([A-Z]\w*)",
    re.M,
)


def test_keyboard_target(keyboard):
    assert keyboard["type"] == "app-extension"
    assert keyboard["platform"] == "iOS"
    base = keyboard["settings"]["base"]
    assert base["PRODUCT_BUNDLE_IDENTIFIER"] == "$(BUNDLE_ID_PREFIX).app.keyboard"
    assert base["TARGETED_DEVICE_FAMILY"] == "1"
    assert base["APPLICATION_EXTENSION_API_ONLY"] is True
    # No app-only code paths: the app target alone sets SHOUTABOUT_APP.
    assert "SWIFT_ACTIVE_COMPILATION_CONDITIONS" not in base
    entitlements = keyboard["entitlements"]
    assert entitlements["path"] == "Config/ShoutAboutKeyboard.entitlements"
    assert entitlements["properties"]["com.apple.security.application-groups"] == [
        "$(APP_GROUP_ID)"
    ]


def test_keyboard_info_plist(keyboard):
    info = keyboard["info"]
    assert info["path"] == "Config/ShoutAboutKeyboard-Info.plist"
    props = info["properties"]
    assert props["CFBundleDisplayName"] == "ShoutAbout"
    assert props["ShoutAboutAppGroup"] == "$(APP_GROUP_ID)"  # AppGroup.id reads it
    extension = props["NSExtension"]
    assert extension["NSExtensionPointIdentifier"] == "com.apple.keyboard-service"
    assert extension["NSExtensionPrincipalClass"] == "$(PRODUCT_MODULE_NAME).KeyboardViewController"
    assert extension["NSExtensionAttributes"] == {
        "IsASCIICapable": False,
        "PrefersRightToLeft": False,
        "PrimaryLanguage": "en-US",
        "RequestsOpenAccess": True,  # the App Group
    }
    controller = (IOS / "ShoutAboutKeyboard" / "KeyboardViewController.swift").read_text("utf-8")
    assert re.search(r"\bclass KeyboardViewController: UIInputViewController\b", controller)


def test_keyboard_compiles_only_small_shared_pieces(keyboard):
    paths = [source["path"] for source in keyboard["sources"]]
    assert paths[0] == "ShoutAboutKeyboard"
    shared = [path for path in paths[1:] if path.endswith(".swift")]
    # Single files from Shared, never the whole folder, the app, the intents or ActivityKit.
    assert all(path.startswith(("Shared/Core/", "Shared/UI/")) for path in shared), shared
    assert "Shared/Core/SharedSettings.swift" not in shared  # the app's settings model
    for source in keyboard["sources"]:
        assert (IOS / source["path"]).exists(), source["path"]
    privacy = [s for s in keyboard["sources"] if s["path"].endswith(".xcprivacy")]
    assert privacy == [
        {"path": "Privacy/Keyboard/PrivacyInfo.xcprivacy", "buildPhase": "resources"}
    ]


def test_keyboard_sources_only_use_types_they_compile(keyboard):
    """A project type used by a keyboard file must be declared in a file the keyboard compiles
    (otherwise the target fails to build). Apple's types aren't declared in ios/, so they pass."""
    declared_anywhere: dict[str, set[str]] = {}
    for path in IOS.rglob("*.swift"):
        for name in TOP_LEVEL_DECLARATION.findall(code_only(path.read_text("utf-8"))):
            declared_anywhere.setdefault(name, set()).add(str(path.relative_to(IOS)))
    files = keyboard_swift_files(keyboard)
    sources = {path: code_only(path.read_text("utf-8")) for path in files}
    declared_here = {name for code in sources.values() for name in DECLARATION.findall(code)}
    problems = []
    for path, code in sources.items():
        for name in sorted(set(re.findall(r"\b[A-Z]\w*\b", code))):
            if name in declared_anywhere and name not in declared_here:
                problems.append(
                    f"{path.relative_to(IOS)} uses {name} from {sorted(declared_anywhere[name])}"
                )
    assert not problems, "\n".join(problems)


def test_keyboard_never_goes_online_or_uses_the_shared_application():
    for path in (IOS / "ShoutAboutKeyboard").rglob("*.swift"):
        code = code_only(path.read_text("utf-8"))
        for banned in (
            "URLSession",
            "UIApplication.shared",
            "NWConnection",
            "print(",
            "os_log",
            "Logger(",
        ):
            assert banned not in code, f"{path.name}: {banned}"


def test_the_app_opens_from_the_keyboard(project):
    schemes = project["targets"]["ShoutAbout"]["info"]["properties"]["CFBundleURLTypes"][0][
        "CFBundleURLSchemes"
    ]
    assert schemes == ["shoutabout"]
    controller = (IOS / "ShoutAboutKeyboard" / "KeyboardViewController.swift").read_text("utf-8")
    assert '"shoutabout://keyboard"' in controller
    model = (IOS / "ShoutAbout" / "App" / "AppModel.swift").read_text("utf-8")
    assert 'case "keyboard":' in model


def test_keyboard_privacy_manifest():
    manifest = plistlib.loads((IOS / "Privacy" / "Keyboard" / "PrivacyInfo.xcprivacy").read_bytes())
    assert manifest["NSPrivacyTracking"] is False
    assert manifest["NSPrivacyTrackingDomains"] == []
    assert manifest["NSPrivacyCollectedDataTypes"] == []
    assert manifest["NSPrivacyAccessedAPITypes"] == [
        {
            "NSPrivacyAccessedAPIType": "NSPrivacyAccessedAPICategoryUserDefaults",
            "NSPrivacyAccessedAPITypeReasons": ["1C8F.1"],
        }
    ]


def test_darwin_names_match_between_the_two_sides():
    bridge = (IOS / "Shared" / "Core" / "KeyboardBridge.swift").read_text("utf-8")
    names = re.findall(r'case \w+ = "(fyi\.shoutabout\.keyboard\.\w+)"', bridge)
    assert sorted(names) == [
        "fyi.shoutabout.keyboard.result",
        "fyi.shoutabout.keyboard.start",
        "fyi.shoutabout.keyboard.state",
        "fyi.shoutabout.keyboard.stop",
    ]
