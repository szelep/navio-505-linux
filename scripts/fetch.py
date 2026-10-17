#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Download and verify the pinned sources, unpack them and patch Linux.

Nothing is used before its SHA256 matches. Existing verified downloads are
reused; --from DIR copies tarballs from a local directory instead of the
network. The kernel tree is unpacked fresh and patched exactly once.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

from common import (BUSYBOX_SRC, DOWNLOADS, LINUX_SRC, ROOT, SOURCES, TOOLCHAIN,
                    TOOLCHAIN_NAME, sha256)

PATCHES = sorted((ROOT / 'kernel/patches').glob('*.patch'))


def fetch(name, local):
    info = SOURCES[name]
    target = DOWNLOADS / name
    if target.is_file() and sha256(target) == info['sha256']:
        print(f'OK      {name}')
        return target
    partial = target.with_suffix(target.suffix + '.part')
    if local:
        print(f'COPY    {name} from {local}')
        shutil.copyfile(local / name, partial)
    else:
        print(f'GET     {info["url"]}')
        with urllib.request.urlopen(info['url']) as response, partial.open('wb') as stream:
            shutil.copyfileobj(response, stream)
    actual = sha256(partial)
    if actual != info['sha256']:
        partial.unlink()
        raise SystemExit(f'SHA256 mismatch for {name}: {actual}')
    partial.replace(target)
    print(f'OK      {name}')
    return target


def unpack(archive, parent, result):
    if result.exists():
        print(f'EXISTS  {result.relative_to(ROOT)} (kept)')
        return False
    parent.mkdir(parents=True, exist_ok=True)
    print(f'UNPACK  {archive.name} -> {result.relative_to(ROOT)}')
    subprocess.run(['tar', '-xf', str(archive), '-C', str(parent)], check=True)
    if not result.is_dir():
        raise SystemExit(f'{archive.name} did not create {result}')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='local', type=Path,
                        help='directory that already contains the pinned tarballs')
    args = parser.parse_args()
    DOWNLOADS.mkdir(exist_ok=True)
    archives = {name: fetch(name, args.local) for name in SOURCES}
    unpack(archives[TOOLCHAIN_NAME + '.tar.xz'], TOOLCHAIN, TOOLCHAIN / TOOLCHAIN_NAME)
    unpack(archives['busybox-1.37.0.tar.bz2'], BUSYBOX_SRC.parent, BUSYBOX_SRC)
    if unpack(archives['linux-6.12.tar.xz'], LINUX_SRC.parent, LINUX_SRC):
        for patch in PATCHES:
            print(f'PATCH   {patch.name}')
            subprocess.run(['patch', '-p1', '--forward', '--quiet', '-d', str(LINUX_SRC),
                            '-i', str(patch)], check=True)
    else:
        for patch in PATCHES:
            check = subprocess.run(['patch', '-p1', '-R', '--dry-run', '--quiet', '-d',
                                    str(LINUX_SRC), '-i', str(patch)], capture_output=True)
            if check.returncode:
                raise SystemExit(f'{LINUX_SRC} exists but {patch.name} is not applied; '
                                 'remove the directory and run "make fetch" again')
    print('Sources ready.')


if __name__ == '__main__':
    sys.exit(main())
