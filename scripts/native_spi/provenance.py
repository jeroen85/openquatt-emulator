"""Record the emulator checkout and build inputs declared for a factory image."""

import re
import subprocess


def firmware_source(revision, config):
    if revision is None and config is None:
        return None
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Geef de volledige firmware-commit op met --firmware-revision")
    if (not isinstance(config, str) or not re.fullmatch(r"configs/[A-Za-z0-9_./-]+\.yaml", config)
            or any(part in ("", ".", "..") for part in config.split("/"))):
        raise ValueError("Geef het repopad van de firmwareconfig op met --firmware-config")
    return {"revision": revision, "config": config, "source": "declared"}


def emulator_checkout(root):
    def git(*args):
        return subprocess.check_output(
            ("git", "--no-optional-locks", "-C", str(root), *args), stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()

    try:
        if git("rev-parse", "--show-prefix"):
            return {"revision": None, "dirty": None}
        revision = git("rev-parse", "--verify", "HEAD")
        return {"revision": revision, "dirty": bool(git("status", "--porcelain", "--untracked-files=normal"))}
    except (OSError, subprocess.SubprocessError):
        return {"revision": None, "dirty": None}
