#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Put the Linux image on a mounted FAT16 SD card for EBOOT, with backup.

  ram      write NK.nb0            -> for EBOOT menu D (run from RAM, NAND untouched)
  nand     write NK.nb0 + XIP.BIN  -> for EBOOT menu X (install to NAND)
  check    show what EBOOT would pick from the card; change nothing
  restore  put back the files saved by an earlier ram/nand run

EBOOT 0.1104 reads only FAT16 and matches 8.3 short names loosely: for a
query like NK.NB0 every short name whose base *starts with* NK and whose
extension is NB0 matches (NKORIG~1.NB0 too), and the first one in directory
order wins. This script reads the real short names from the mounted
filesystem (VFAT_IOCTL_READDIR_BOTH, read-only, no root needed) and refuses
to continue unless exactly one file matches each name EBOOT will open.
Files that get replaced are first copied to --backup on this computer.
It never talks to the device, UART or NAND.
"""
import argparse
import ctypes
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, ROOT, sha256  # noqa: E402

TEMP = 'NAVIOIMG.TMP'


def eboot_matches(query, short):
    """EBOOT 0.1104 file name comparator (0x83e26c80) on an 11-byte short name."""
    base, dot, ext = query.upper().partition('.')
    name, extension = short[:8].rstrip(' '), short[8:11].rstrip(' ')
    if dot:
        return name.startswith(base) and extension == ext
    return name == base and extension == ''


def root_entries(card):
    """(short 11-char name, long name, is_dir) for the card's root directory."""
    class Dirent(ctypes.Structure):
        _fields_ = [('ino', ctypes.c_long), ('off', ctypes.c_longlong),
                    ('reclen', ctypes.c_ushort), ('name', ctypes.c_char * 256)]
    size = ctypes.sizeof(Dirent)
    request = 0x80000000 | (size * 2) << 16 | ord('r') << 8 | 1  # VFAT_IOCTL_READDIR_BOTH
    entries = []
    fd = os.open(card, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while True:
            buffer = bytearray(size * 2)
            fcntl.ioctl(fd, request, buffer, True)
            short = Dirent.from_buffer_copy(buffer[:size])
            long = Dirent.from_buffer_copy(buffer[size:])
            if not short.reclen:
                break
            alias = bytes(short.name).decode('ascii')
            if alias in ('.', '..'):
                continue
            name = bytes(long.name).decode('utf-8') if long.reclen else alias
            base, _, ext = alias.partition('.')
            entries.append((base.upper().ljust(8) + ext.upper().ljust(3), name,
                            (card / name).is_dir()))
    finally:
        os.close(fd)
    return entries


def matches(card, query):
    return [(short, name) for short, name, is_dir in root_entries(card)
            if not is_dir and eboot_matches(query, short)]


def identify(card):
    found = json.loads(subprocess.check_output(
        ['findmnt', '-J', '-M', str(card), '-o', 'SOURCE,TARGET,UUID,FSTYPE,LABEL'],
        text=True))['filesystems'][0]
    if found['fstype'] != 'vfat':
        raise SystemExit(f'{card} is {found["fstype"]}, not FAT')
    version = subprocess.check_output(['lsblk', '-n', '-o', 'FSVER', found['source']],
                                      text=True).strip()
    if version != 'FAT16':
        raise SystemExit(f'{found["source"]} is {version or "unknown"}; EBOOT needs FAT16 '
                         '(it follows FAT32 cluster chains incorrectly)')
    return found


def report(card):
    for query in ['NK.NB0', 'XIP.BIN', 'UPD', 'EBOOT.NB0']:
        hits = matches(card, query)
        shown = ', '.join(f'{name} [{short[:8].rstrip()}.{short[8:].rstrip()}]'
                          for short, name in hits) or '-'
        print(f'  EBOOT {query:<10} -> {shown}')


def single_target(card, query, default):
    hits = matches(card, query)
    if len(hits) > 1:
        names = ', '.join(name for _, name in hits)
        raise SystemExit(f'More than one file matches {query} for EBOOT: {names}. '
                         'Rename or move the extra copies (keep backups off the card '
                         'or under a non-matching name such as *.SAV) and retry.')
    return hits[0][1] if hits else default


def write_verified(card, data_path, name, expected):
    temp = card / TEMP
    if temp.exists():
        raise SystemExit(f'{temp} exists from an interrupted run; inspect and delete it')
    with data_path.open('rb') as source, temp.open('xb') as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    if sha256(temp) != expected:
        temp.unlink()
        raise SystemExit(f'Write verification failed for {name}')
    os.replace(temp, card / name)


def stage(args):
    card = args.card.resolve()
    mount = identify(card)
    image = args.image.resolve()
    expected = sha256(image)
    print(f'Card {mount["source"]} UUID {mount["uuid"]} label {mount["label"]} at {card}')
    print('Before:')
    report(card)
    if matches(card, 'UPD'):
        raise SystemExit('A file matching UPD is on the card: EBOOT would enter update mode. '
                         'Remove it first.')
    targets = {'NK.NB0': single_target(card, 'NK.NB0', 'NK.nb0')}
    if args.mode == 'nand':
        targets['XIP.BIN'] = single_target(card, 'XIP.BIN', 'XIP.BIN')
    needed = image.stat().st_size * len(targets)
    if shutil.disk_usage(card).free + sum((card / n).stat().st_size for n in targets.values()
                                          if (card / n).exists()) < needed + image.stat().st_size:
        raise SystemExit('Not enough free space on the card')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = args.backup.resolve() / stamp
    backup.mkdir(parents=True)
    saved, created = {}, []
    for name in targets.values():
        if (card / name).exists():
            shutil.copy2(card / name, backup / name)
            saved[name] = sha256(backup / name)
            if sha256(card / name) != saved[name]:
                raise SystemExit(f'Backup of {name} differs; card left unchanged')
        else:
            created.append(name)
    manifest = {'card': mount, 'mode': args.mode, 'image': str(image), 'image_sha256': expected,
                'saved': saved, 'created': created, 'status': 'BACKED UP'}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name in targets.values():
        write_verified(card, image, name, expected)
    subprocess.run(['sync', '-f', str(card)], check=True)
    print('After:')
    report(card)
    for query, name in targets.items():
        hits = matches(card, query)
        assert [n for _, n in hits] == [name], f'EBOOT would not pick {name} for {query}: {hits}'
        assert sha256(card / name) == expected, f'{name} on card differs from the image'
    assert not matches(card, 'UPD')
    manifest['status'] = 'STAGED'
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'\nStaged {image.name} (sha256 {expected}) as {", ".join(targets.values())}.')
    print(f'Backup and undo information: {backup}')
    print(f'Restore later with: {Path(sys.argv[0]).name} restore --card {card} --backup {backup}')
    print('Unmount the card (e.g. "udisksctl unmount -b /dev/sdX1") and move it to the Navio.')


def restore(args):
    card = args.card.resolve()
    identify(card)
    backup = args.backup.resolve()
    manifest = json.loads((backup / 'manifest.json').read_text())
    for name, digest in manifest['saved'].items():
        if sha256(backup / name) != digest:
            raise SystemExit(f'Backup copy of {name} is damaged; card left unchanged')
    for name, digest in manifest['saved'].items():
        write_verified(card, backup / name, name, digest)
        print(f'Restored {name}')
    for name in manifest['created']:
        if (card / name).exists() and sha256(card / name) == manifest['image_sha256']:
            (card / name).unlink()
            print(f'Removed {name} (was added by staging)')
    subprocess.run(['sync', '-f', str(card)], check=True)
    report(card)


def check(args):
    card = args.card.resolve()
    mount = identify(card)
    print(f'Card {mount["source"]} UUID {mount["uuid"]} label {mount["label"]}: FAT16 OK')
    report(card)
    if args.image:
        expected = sha256(args.image)
        for query in ['NK.NB0', 'XIP.BIN']:
            for _, name in matches(card, query):
                state = 'identical to' if sha256(card / name) == expected else 'differs from'
                print(f'  {name}: {state} {args.image}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for mode in ['ram', 'nand']:
        p = sub.add_parser(mode, help=f'stage the image for EBOOT menu {"D" if mode == "ram" else "X"}')
        p.add_argument('--card', type=Path, required=True, help='mount point of the SD card')
        p.add_argument('--image', type=Path, default=OUT / 'NK.nb0')
        p.add_argument('--backup', type=Path, default=ROOT / 'backups')
        p.set_defaults(func=stage, mode=mode)
    p = sub.add_parser('restore', help='undo a staging run from its backup directory')
    p.add_argument('--card', type=Path, required=True)
    p.add_argument('--backup', type=Path, required=True, help='backups/<timestamp> directory')
    p.set_defaults(func=restore)
    p = sub.add_parser('check', help='report what EBOOT would load; read-only')
    p.add_argument('--card', type=Path, required=True)
    p.add_argument('--image', type=Path, help='also compare matched files with this image')
    p.set_defaults(func=check)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
