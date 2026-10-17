# SPDX-License-Identifier: MIT
"""Shared paths, pinned sources and toolchain lookup for the build scripts."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / 'downloads'
TOOLCHAIN = ROOT / 'toolchain'
BUILD = ROOT / 'build'
OUT = ROOT / 'out'

LINUX_SRC = BUILD / 'linux-6.12'
LINUX_OUT = BUILD / 'kernel'
BUSYBOX_SRC = BUILD / 'busybox-1.37.0'
BUSYBOX_OUT = BUILD / 'busybox'
INITRAMFS_OUT = BUILD / 'initramfs'

TOOLCHAIN_NAME = 'armv5-eabi--musl--stable-2024.05-1'

# Every download is checked against this SHA256 before it is used.
SOURCES = {
    'linux-6.12.tar.xz': {
        'url': 'https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz',
        'sha256': 'b1a2562be56e42afb3f8489d4c2a7ac472ac23098f1ef1c1e40da601f54625eb',
    },
    'busybox-1.37.0.tar.bz2': {
        'url': 'https://busybox.net/downloads/busybox-1.37.0.tar.bz2',
        'sha256': '3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4',
    },
    TOOLCHAIN_NAME + '.tar.xz': {
        'url': 'https://toolchains.bootlin.com/downloads/releases/toolchains/armv5-eabi/'
               'tarballs/' + TOOLCHAIN_NAME + '.tar.xz',
        'sha256': '9825d9a4515a92d2251aab3228ddbb6258440b0f952b95fcaa94653a4b29c71a',
    },
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def cross_prefix():
    """Return the cross-compiler prefix, e.g. .../bin/arm-linux-."""
    compiler = TOOLCHAIN / TOOLCHAIN_NAME / 'bin/arm-linux-gcc'
    if not compiler.is_file():
        raise SystemExit(f'Missing {compiler}; run "make fetch" first')
    return str(compiler)[:-3]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')
