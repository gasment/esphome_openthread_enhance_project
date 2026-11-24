"""Copy the SDK's OpenThread module and apply the device's patch to the copy."""
import filecmp
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def copy_if_changed(source, destination):
    if not Path(destination).is_file() or not filecmp.cmp(source, destination, shallow=False):
        shutil.copy2(source, destination)


def prepare(source: Path, destination: Path, patch: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=True)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("The OpenThread copy must be separate from the installed SDK")
    patched_files = {
        Path(line.removeprefix("+++ b/"))
        for line in patch.read_text(encoding="utf-8").splitlines()
        if line.startswith("+++ b/")
    }
    if not patched_files or any(path.is_absolute() or ".." in path.parts for path in patched_files):
        raise ValueError("Invalid OpenThread patch paths")

    def ignore(directory, names):
        relative = Path(directory).relative_to(source)
        return {
            name for name in names
            if name == ".git" or relative / name in patched_files or relative / name == Path("zephyr/module.yml")
        }

    with TemporaryDirectory(prefix="poll-patch-", dir=destination) as temporary:
        staging = Path(temporary)
        for path in patched_files:
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / path, target)
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(destination)}
        command = ["git", "apply", "--whitespace=error", str(patch.resolve())]
        subprocess.run(command, cwd=staging, env=env, check=True)
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore, copy_function=copy_if_changed)
        for path in patched_files:
            copy_if_changed(staging / path, destination / path)


if __name__ == "__main__":
    prepare(*(Path(value) for value in sys.argv[1:]))
