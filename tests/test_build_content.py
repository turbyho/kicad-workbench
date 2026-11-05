"""Tests of the files of the sdist and of the wheel for PyPI.

The tests build the two with hatchling (the build backend of pyproject.toml).
The repository has private files (the paths of .git/github-ignore, for example
plans/, spike/ and CLAUDE.md), and these must not go to PyPI. The sdist must
have all files that a build and the tests need, and no file that git does not
track (for example a local test design that is not in .gitignore).

Git finds the private files with the pathspecs of scripts/github-publish.sh and
of the pre-push hook: the tests do not have a second copy of the match rules.
"""

import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from kicad_workbench import __version__

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kicad_workbench"
# The files that hatchling adds to each sdist: the metadata, and .gitignore (the
# ignore rules of the sdist).
SDIST_ADDED = {"PKG-INFO", ".gitignore"}
# The pathspec magic of the environment changes the meaning of a pathspec. The
# publish script and the hook remove these variables too.
PATHSPEC_ENV = (
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, set[str]]:
    """The files of the sdist and of the wheel, relative to their root."""
    pytest.importorskip("hatchling")
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder

    out = tmp_path_factory.mktemp("dist")
    (sdist,) = SdistBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])
    (wheel,) = WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])
    with tarfile.open(sdist) as tar:
        # Each member name starts with "<name>-<version>/".
        sdist_files = {m.name.split("/", 1)[1] for m in tar.getmembers() if m.isfile()}
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = set(archive.namelist())
    return {"sdist": sdist_files, "wheel": wheel_files}


def git_files(*pathspecs: str) -> set[str]:
    """The files that git tracks, relative to ROOT. A skip when ROOT is not the top
    level of a git repository (for example an unpacked sdist in a different
    repository): then ls-files gives no files of this project."""
    env = {k: v for k, v in os.environ.items() if k not in PATHSPEC_ENV}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True, env=env
        ).stdout

    try:
        top = git("rev-parse", "--show-toplevel").strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no git repository")
    if Path(top).resolve() != ROOT:
        pytest.skip(f"the git repository of {ROOT} starts at {top}")
    return set(git("ls-files", "-z", "--", *pathspecs).split("\0")) - {""}


def private_pathspecs() -> list[str]:
    """The pathspecs of the private paths, as read_list and the pathspec block of
    scripts/github-publish.sh and .git/hooks/pre-push make them: the text after
    "#" and the outer spaces go, one "/" at the end goes, and a pattern p gives
    "p", "p/*", "*/p" and "*/p/*" relative to the top level."""
    path = ROOT / ".git" / "github-ignore"
    if not path.is_file():
        pytest.skip("no .git/github-ignore: this clone has no list of private paths")
    pathspecs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        p = line.split("#", 1)[0].strip().removesuffix("/")
        if not p:
            continue
        parts = f"/{p}/".split("/")[1:-1]
        assert parts and "." not in parts and ".." not in parts, f"pattern {p!r} not permitted"
        pathspecs += [f":(top){p}", f":(top){p}/*", f":(top)*/{p}", f":(top)*/{p}/*"]
    assert pathspecs, f"{path} has no patterns"
    return pathspecs


def test_sdist_has_only_files_of_git(built):
    """No file that git does not track goes to PyPI."""
    assert built["sdist"] - git_files() - SDIST_ADDED == set()


def test_no_private_file_in_the_sdist(built):
    # The sdist has only files of git (the test above), thus the private files of
    # git are all private files that it can have.
    private = git_files(*private_pathspecs())
    assert private, "no private file found: the check of the patterns does not work"
    assert built["sdist"] & private == set()


def test_sdist_has_the_source_and_the_tests(built):
    """All files of git under src, tests and docs, for a build and a test run, less
    the exclude list of the sdist and the private files."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11 or later

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    expected = git_files("src", "tests", "docs") - git_files(*(f":(top){e}" for e in excluded))
    if (ROOT / ".git" / "github-ignore").is_file():
        expected -= git_files(*private_pathspecs())
    assert len(expected) > 50
    assert expected - built["sdist"] == set()


def test_wheel_has_the_package_only(built):
    """The files of the package that git tracks, and the metadata. No other file."""
    package = {f.removeprefix("src/") for f in git_files(f"src/{PACKAGE}")}
    assert len(package) > 50
    metadata = {f for f in built["wheel"] if f.startswith(f"{PACKAGE}-{__version__}.dist-info/")}
    assert built["wheel"] - metadata == package
