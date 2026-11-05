import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "__init__.py",
    "osm_parser.py",
    "osm_parser_dialog.py",
    "resources.py",
    "metadata.txt",
    "icon.svg",
    "Welcome.svg",
    "README.md",
    "RELEASE_NOTES.md",
    "LICENSE",
)
EXTENSIONS = {".py", ".ui", ".svg", ".png", ".qss"}


def build(destination):
    files = [ROOT / name for name in FILES if (ROOT / name).is_file()]
    files.extend(
        path
        for path in (ROOT / "src").rglob("*")
        if path.is_file()
        and path.suffix in EXTENSIONS
        and "__pycache__" not in path.parts
    )
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            relative = path.relative_to(ROOT)
            archive.write(path, f"osm_parser/{relative.as_posix()}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest}  {destination.name}\n"
    )
    return destination, digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build a local installable Aino QGIS archive without deploying it."
    )
    parser.add_argument("destination")
    args = parser.parse_args()
    output, digest = build(args.destination)
    print(f"{output}\nSHA256 {digest}")
