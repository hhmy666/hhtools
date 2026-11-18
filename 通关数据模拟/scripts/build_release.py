"""Build the portable colleague package without runtime settings or caches."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def build(destination=None):
    destination = Path(destination) if destination else ROOT / '发布包' / '通关数据模拟.zip'
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = [ROOT / '启动通关数据模拟.command']
    for name in ('frontend', 'scripts', 'docs', 'resources'):
        files.extend(path for path in (ROOT / name).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts
                     and path.suffix != '.pyc' and path.name != '.DS_Store')
    with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, str(Path(ROOT.name) / path.relative_to(ROOT)))
    return destination


if __name__ == '__main__':
    print(build())
