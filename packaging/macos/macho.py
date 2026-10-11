"""Find the code inside a macOS app bundle, in the order it has to be signed.

Notarization needs every Mach-O file in ShoutAbout.app signed with the Developer ID
certificate and a secure timestamp, and the app's main executable signed with the Hardened
Runtime and its entitlements. Apple's rules (Creating distribution-signed code for macOS):
sign from the inside out, don't sign with ``codesign --deep``, and put entitlements only
on executables, never on library code. A PyInstaller app holds a few hundred loose Mach-O
files (Python extension modules, dylibs, libpython) and, today, no nested bundles; nested
``.framework``/``.bundle``/... directories are listed too, after their contents, in case a
wheel ever ships one.

    python3 packaging/macos/macho.py list ShoutAbout.app             # kind<TAB>path
    python3 packaging/macos/macho.py list --kind lib -0 ShoutAbout.app  # for xargs -0

Kinds, in signing order:

* ``lib``: dylibs, Python extension modules (MH_BUNDLE) and other loadable code. Signed
  with a timestamp, no entitlements, in any order (none contains another).
* ``exe``: a helper executable other than the app's own. Hardened Runtime, no
  entitlements (ShoutAbout ships none; a wheel could).
* ``bundle``: a nested bundle directory, signed after everything inside it, deepest first.

The app's main executable (Info.plist ``CFBundleExecutable``) is left out: signing the
.app itself signs it, with the entitlements. Symbolic links are skipped (PyInstaller
cross-links Contents/Frameworks and Contents/Resources), so each file appears once.
Object files and debug symbols (MH_OBJECT, MH_DSYM...) can't be signed and aren't needed
at run time: ``list`` reports them on stderr and leaves them out.

Standard library only: it runs with the build's Python or the system's python3.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Mach-O file types (mach-o/loader.h) that codesign signs.
MH_EXECUTE = 0x2
MH_DYLIB = 0x6
MH_DYLINKER = 0x7
MH_BUNDLE = 0x8
SIGNABLE = {MH_EXECUTE: "exe", MH_DYLIB: "lib", MH_DYLINKER: "lib", MH_BUNDLE: "lib"}

# Thin headers, as the first four bytes on disk, and the byte order of the rest.
THIN_MAGICS = {
    b"\xcf\xfa\xed\xfe": "<",  # MH_MAGIC_64, little endian (arm64, x86_64)
    b"\xce\xfa\xed\xfe": "<",  # MH_MAGIC, little endian
    b"\xfe\xed\xfa\xcf": ">",  # big endian 64
    b"\xfe\xed\xfa\xce": ">",  # big endian 32
}
FAT_MAGIC = b"\xca\xfe\xba\xbe"  # universal binary (always big endian)
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
# Java class files share 0xCAFEBABE; their next field is the class version (45 or more),
# while a universal binary's is its architecture count (a handful).
MAX_FAT_ARCHS = 30

BUNDLE_SUFFIXES = (".framework", ".bundle", ".plugin", ".app", ".appex", ".xpc", ".kext")


@dataclass(frozen=True)
class CodeItem:
    kind: str  # "lib" | "exe" | "bundle"
    path: Path

    @property
    def depth(self) -> int:
        return len(self.path.parts)


def _thin_filetype(head: bytes) -> int | None:
    order = THIN_MAGICS.get(head[:4])
    if order is None or len(head) < 16:
        return None
    return int(struct.unpack_from(order + "I", head, 12)[0])


def macho_filetype(path: Path) -> int | None:
    """The Mach-O file type of ``path`` (of its first slice if universal), or None if it
    isn't a Mach-O file."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:4] in THIN_MAGICS:
                return _thin_filetype(head)
            if head[:4] not in (FAT_MAGIC, FAT_MAGIC_64) or len(head) < 8:
                return None
            count = struct.unpack_from(">I", head, 4)[0]
            if not 0 < count <= MAX_FAT_ARCHS:
                return None  # a Java class file, not code
            if head[:4] == FAT_MAGIC:  # fat_arch: cputype, cpusubtype, offset, size, align
                offset = struct.unpack_from(">iiIII", head, 8)[2]
            else:  # fat_arch_64: cputype, cpusubtype, offset, size (64 bit), align, reserved
                offset = struct.unpack_from(">iiQQII", head, 8)[2]
            f.seek(offset)
            return _thin_filetype(f.read(16))
    except (OSError, struct.error):
        return None


def main_executable(app: Path) -> Path:
    """The bundle's main executable: Contents/MacOS/<CFBundleExecutable>."""
    name = app.stem
    info = app / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as f:
            name = str(plistlib.load(f).get("CFBundleExecutable") or name)
    except (OSError, plistlib.InvalidFileException):
        pass
    return app / "Contents" / "MacOS" / name


def _is_bundle_dir(path: Path) -> bool:
    return path.name.endswith(BUNDLE_SUFFIXES) and not path.is_symlink() and path.is_dir()


def walk(app: Path, warn=None) -> Iterator[CodeItem]:
    """Every code item inside ``app`` (not the app itself), unordered."""
    app = Path(app)
    main = main_executable(app)
    for dirpath, dirnames, filenames in os.walk(app / "Contents", followlinks=False):
        base = Path(dirpath)
        for d in dirnames:
            if _is_bundle_dir(base / d):
                yield CodeItem("bundle", base / d)
        for name in filenames:
            path = base / name
            if path.is_symlink() or path == main:
                continue
            filetype = macho_filetype(path)
            if filetype is None:
                continue
            kind = SIGNABLE.get(filetype)
            if kind is None:
                if warn is not None:
                    warn(f"not signed (Mach-O type {filetype:#x}, not loadable code): {path}")
                continue
            yield CodeItem(kind, path)


def signing_order(items: list[CodeItem]) -> list[CodeItem]:
    """Inside out: loose libraries, then helper executables, then nested bundles deepest
    first (a bundle's contents are always loose files signed before it)."""
    rank = {"lib": 0, "exe": 1, "bundle": 2}
    return sorted(items, key=lambda i: (rank[i.kind], -i.depth, str(i.path)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list the code to sign, in signing order")
    ls.add_argument("app", type=Path)
    ls.add_argument("--kind", choices=("lib", "exe", "bundle"), help="only this kind, paths only")
    ls.add_argument("-0", dest="nul", action="store_true", help="NUL-separated paths (xargs -0)")
    args = parser.parse_args(argv)

    if not (args.app / "Contents").is_dir():
        print(f"not an app bundle: {args.app}", file=sys.stderr)
        return 2
    items = signing_order(list(walk(args.app, warn=lambda m: print(m, file=sys.stderr))))
    if args.kind:
        items = [i for i in items if i.kind == args.kind]
    end = "\0" if args.nul else "\n"
    out = sys.stdout
    for item in items:
        if args.kind or args.nul:
            out.write(f"{item.path}{end}")
        else:
            out.write(f"{item.kind}\t{item.path}{end}")
    counts = {k: sum(1 for i in items if i.kind == k) for k in ("lib", "exe", "bundle")}
    print(
        f"{len(items)} code items ({counts['lib']} libraries, {counts['exe']} helper "
        f"executables, {counts['bundle']} nested bundles) besides the main executable",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
