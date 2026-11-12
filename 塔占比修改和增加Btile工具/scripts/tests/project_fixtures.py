"""Read optional regression fixtures from a local, ignored directory."""
from pathlib import Path
from unittest import SkipTest

FIXTURES = Path(__file__).with_name("fixtures")


def read_project_fixture(name: str) -> bytes:
    try:
        return (FIXTURES / name).read_bytes()
    except FileNotFoundError:
        raise SkipTest("Local project template fixtures are not included") from None
