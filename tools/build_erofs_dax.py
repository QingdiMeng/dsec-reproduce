"""Publish one uncompressed EROFS/DAX layer from an explicit staged directory."""
import argparse
import json
from pathlib import Path
from dsec.storage.erofs import build_layer


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--mkfs',default='mkfs.erofs')
    args=parser.parse_args()
    print(json.dumps(build_layer(args.source,args.out,mkfs=args.mkfs)))


if __name__=='__main__':main()
