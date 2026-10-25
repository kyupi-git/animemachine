"""Wrap a release asset in a single uploadable, checksummed offline package."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from animemachine.offline_update import create


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asset', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--kind', choices=('docker', 'portable'), required=True)
    args = parser.parse_args()
    create(args.asset, args.output, version=args.version, kind=args.kind)


if __name__ == '__main__':
    main()
