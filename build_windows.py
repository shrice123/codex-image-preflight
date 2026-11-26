"""Build the standalone Windows x64 executable and its distribution files."""

import hashlib
import importlib.metadata as metadata
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
EXECUTABLE = "Codex-Image-Prep-Windows-x64.exe"


def collect_licenses(destination: Path) -> None:
    shutil.copytree(ROOT / "third_party", destination)
    for name in ("Pillow", "pywin32", "PyInstaller", "packaging", "setuptools"):
        package = metadata.distribution(name)
        copied = 0
        for entry in package.files or ():
            parts = entry.parts
            if parts[0].endswith(".dist-info") and len(parts) > 2 and parts[1] == "licenses":
                relative = Path(*parts[2:])
            elif entry.name.lower().startswith(("license", "copying")):
                relative = Path(entry.name)
            else:
                continue
            target = destination / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package.locate_file(entry), target)
            copied += 1
        if not copied:
            raise RuntimeError(f"No license files found for {name}")


def build(output: Path) -> None:
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("Build with 64-bit Windows Python.")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="codex-image-prep-build-") as temp:
        folder = Path(temp)
        licenses = folder / "Licenses"
        collect_licenses(licenses)
        executable = output / EXECUTABLE
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
                "--noupx", "--name", executable.stem, "--distpath", str(output),
                "--specpath", str(folder), "--workpath", str(folder / "work"),
                "--add-data", f"{licenses}:Licenses", str(ROOT / "smart_paste.py"),
            ],
            cwd=ROOT,
            check=True,
        )
        subprocess.run([str(executable), "--self-test"], check=True, timeout=40)
        portable = output / f"{executable.stem}.zip"
        with ZipFile(portable, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.write(executable, executable.name)
            bundle.write(ROOT / "Portable-Readme.txt", "使用说明.txt")
            for license_file in sorted(licenses.rglob("*")):
                if license_file.is_file():
                    bundle.write(license_file, license_file.relative_to(folder).as_posix())
        with ZipFile(portable) as bundle:
            bad_entry = bundle.testzip()
            if bad_entry:
                raise RuntimeError(f"Cannot validate archive entry {bad_entry}")
        with (output / "SHA256SUMS.txt").open("w", encoding="ascii", newline="\n") as sums:
            for path in (executable, portable):
                with path.open("rb") as artifact:
                    digest = hashlib.file_digest(artifact, "sha256").hexdigest()
                sums.write(f"{digest}  {path.name}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python build_windows.py OUTPUT_DIRECTORY")
    build(Path(sys.argv[1]).resolve())
