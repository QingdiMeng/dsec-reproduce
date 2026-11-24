"""Publish shared EROFS directories and a lean task archive from a pinned image."""
import argparse
import json
from pathlib import Path
from dsec_image.split import split


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--layer-root',required=True,type=Path)
    parser.add_argument('--shared-dir',required=True,action='append')
    parser.add_argument('--mkfs',default='mkfs.erofs')
    parser.add_argument('--reserve-gib',type=int,default=10)
    args=parser.parse_args()
    print(json.dumps(split(args.image,args.out,args.layer_root,args.shared_dir,mkfs=args.mkfs,reserve_gib=args.reserve_gib)))


if __name__=='__main__':main()
