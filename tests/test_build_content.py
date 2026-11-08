"""Tests of the files of the sdist and of the wheel for PyPI.

The tests build the two with hatchling (the build backend of pyproject.toml).
The repository has private files (the blocked paths of .publish-filter on main,
for example plans/, spike/ and CLAUDE.md), and these must not go to PyPI. The
sdist must have all files that a build and the tests need, and no file that git
does not track (for example a local test design that is not in .gitignore).

The tool git-publish-filter lists the private files (``ls-blocked``): the tests
do not have a second copy of the match rules.
"""

import importlib.metadata
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from kicad_workbench import __version__

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kicad_workbench"


def _test_requirement(name: str) -> Requirement:
    """The requirement of a test dependency in requirements-test.txt."""
    for line in (ROOT / "requirements-test.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and Requirement(line).name == name:
            return Requirement(line)
    raise LookupError(f"requirements-test.txt has no {name}")


# The hatchling of the extra "test" (requirements-test.txt).
HATCHLING = _test_requirement("hatchling")
# The files that hatchling adds to each sdist: the metadata, and .gitignore (the
# ignore rules of the sdist).
SDIST_ADDED = {"PKG-INFO", ".gitignore"}
# The pathspec magic of the environment changes the meaning of a pathspec.
# git-publish-filter removes these variables too.
PATHSPEC_ENV = (
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, set[str]]:
    """The files of the sdist and of the wheel, relative to their root."""
    # hatchling is a test dependency (the extra "test"): a missing or old hatchling is
    # an error of the environment, not a reason to skip the check.
    try:
        found = importlib.metadata.version("hatchling")
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(f"The test needs {HATCHLING} (the extra test): it is not installed")
    if not HATCHLING.specifier.contains(found, prereleases=True):
        pytest.fail(f"The test needs {HATCHLING} (the extra test), not hatchling {found}")
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


def has_private_files() -> bool:
    """Tell if this clone is the private repository: main has .publish-filter.
    A clone of the public repository (GitHub, its CI) has no private files."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", "main:.publish-filter"], capture_output=True, check=False
    )
    return result.returncode == 0


def private_files() -> set[str]:
    """The private files that git tracks, as git-publish-filter selects them.

    A skip when the clone has no private files, or when the tool is not in PATH
    (for example in a CI job of the private remote). An error of the tool is a
    failure, not a skip: a broken configuration must not hide a private file.
    """
    if not has_private_files():
        pytest.skip("no .publish-filter on main: this clone has no private files")
    tool = shutil.which("git-publish-filter")
    if tool is None:
        pytest.skip("git-publish-filter is not in PATH: it gives the list of private files")
    out = subprocess.run([tool, "ls-blocked", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return set(out.decode("utf-8").split("\0")) - {""}


def test_sdist_has_only_files_of_git(built):
    """No file that git does not track goes to PyPI."""
    assert built["sdist"] - git_files() - SDIST_ADDED == set()


def test_no_private_file_in_the_sdist(built):
    # The sdist has only files of git (the test above), thus the private files of
    # git are all private files that it can have.
    private = private_files()
    assert private, "no private file found: the check of the patterns does not work"
    assert built["sdist"] & private == set()


def test_sdist_has_the_source_and_the_tests(built):
    """All files of git under src, tests and docs, for a build and a test run, less
    the exclude list of the sdist and the private files."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11 or later

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    expected = git_files("src", "tests", "docs") - git_files(*(f":(top){e}" for e in excluded))
    if has_private_files():
        expected -= private_files()
    assert len(expected) > 50
    assert expected - built["sdist"] == set()


def test_wheel_has_the_package_only(built):
    """The files of the package that git tracks, and the metadata. No other file."""
    package = {f.removeprefix("src/") for f in git_files(f"src/{PACKAGE}")}
    assert len(package) > 50
    metadata = {f for f in built["wheel"] if f.startswith(f"{PACKAGE}-{__version__}.dist-info/")}
    assert built["wheel"] - metadata == package
