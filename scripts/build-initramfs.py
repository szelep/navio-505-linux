#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile rootfs/init.c and write a deterministic newc initramfs (no root needed)."""
import json
import stat
import subprocess

from common import BUSYBOX_OUT, INITRAMFS_OUT, ROOT, cross_prefix, sha256, write_json


def cpio(entries):
    data = bytearray()
    for ino, (name, mode, payload, major, minor) in enumerate(entries, 1):
        name = name.encode() + b'\0'
        fields = [ino, mode, 0, 0, 1, 0, len(payload), 0, 0, major, minor, len(name), 0]
        data += b'070701' + b''.join(f'{n:08x}'.encode() for n in fields) + name
        data += b'\0' * (-len(data) % 4)
        data += payload
        data += b'\0' * (-len(data) % 4)
    data += b'\0' * (-len(data) % 512)
    return data


def main():
    prefix = cross_prefix()
    INITRAMFS_OUT.mkdir(parents=True, exist_ok=True)
    init = INITRAMFS_OUT / 'init'
    subprocess.run([prefix + 'gcc', '-static', '-Os', '-march=armv5te', '-marm',
                    '-mfloat-abi=soft', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    str(ROOT / 'rootfs/init.c'), '-o', str(init)], check=True)
    report = subprocess.check_output([prefix + 'readelf', '-hAl', str(init)], text=True)
    assert 'ARM' in report and 'Tag_CPU_arch: v5TE' in report
    assert 'INTERP' not in report and 'hard-float ABI' not in report, 'must be static soft-float'
    busybox = BUSYBOX_OUT / 'busybox'
    applets = json.loads((BUSYBOX_OUT / 'manifest.json').read_text())
    assert sha256(busybox) == applets['binary_sha256'], 'BusyBox changed after its build'
    entries = [('.', stat.S_IFDIR | 0o755, b'', 0, 0)]
    entries += [(name, stat.S_IFDIR | 0o755, b'', 0, 0)
                for name in ['dev', 'proc', 'sys', 'tmp', 'bin', 'etc', 'root']]
    entries += [('dev/console', stat.S_IFCHR | 0o600, b'', 5, 1),
                ('init', stat.S_IFREG | 0o755, init.read_bytes(), 0, 0),
                ('bin/busybox', stat.S_IFREG | 0o755, busybox.read_bytes(), 0, 0)]
    for name in sorted(set(applets['applets'] + ['sh'])):
        entries.append(('bin/' + name, stat.S_IFLNK | 0o777, b'busybox', 0, 0))
    entries += [('etc/passwd', stat.S_IFREG | 0o644, b'root::0:0:root:/root:/bin/sh\n', 0, 0),
                ('etc/group', stat.S_IFREG | 0o644, b'root:x:0:\n', 0, 0),
                ('TRAILER!!!', 0, b'', 0, 0)]
    image = cpio(entries)
    target = INITRAMFS_OUT / 'initramfs.cpio'
    target.write_bytes(image)
    write_json(INITRAMFS_OUT / 'manifest.json', {
        'image_bytes': len(image), 'image_sha256': sha256(target),
        'init_sha256': sha256(init), 'busybox_sha256': applets['binary_sha256'],
        'contents': [entry[0] for entry in entries[:-1]]})
    print(f'initramfs: {target} ({len(image)} B)')


if __name__ == '__main__':
    main()
