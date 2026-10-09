"""Build the shared native session/file agent for Linux container deployment."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cc", "-static", "-DDSEC_NATIVE_STANDALONE", "-O2", "-Wall",
                    "-Wextra", "-Werror", "-o", str(args.out), str(args.source.resolve(strict=True))],
                   check=True)


if __name__ == "__main__":
    main()
