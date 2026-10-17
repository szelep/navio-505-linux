#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Wrap Linux Image + DTB + handoff stub into one B000FF image for EBOOT.

EBOOT 0.1104 accepts only Windows CE B000FF images. The same file serves
menu D (load NK.nb0 from SD into RAM and run it) and menu X (write XIP.BIN
to NAND; afterwards EBOOT boots it from NAND without the card).

Physical layout of the single contiguous image (EBOOT VA = PA + 0x60000000):

  0x20300000  branch to the handoff, 'ECEC' at +0x40, ROMHDR pointer at +0x44,
              a synthetic ROMHDR and nk.exe TOC entry for the EBOOT parser
  0x20301000  handoff stub (boot/handoff.ld), private stack up to 0x2030f000
  0x20310000  flattened device tree, outside Linux RAM
  0x20408000  raw Linux Image (PHYS_OFFSET 0x20400000 + TEXT_OFFSET 0x8000)

The low 4 MiB (incl. the live LCD framebuffer at 0x20100000) is never given
to Linux. NAND boot reads BootConfig.ImageLength bytes to 0x80300000 and
jumps there, so the file is one gap-free run of records starting at the
branch, and the launch address equals the image start.
"""
import json
import struct
import subprocess

from common import BUILD, LINUX_OUT, OUT, ROOT, cross_prefix, sha256, write_json

VA = 0x60000000
BASE_PA = 0x20300000
HANDOFF_PA = 0x20301000
DTB_PA = 0x20310000
KERNEL_PA = 0x20408000
RECORD = 0xf000


def branch(source, target):
    offset = target - (source + 8)
    assert offset % 4 == 0 and -(1 << 25) <= offset < (1 << 25)
    return 0xea000000 | ((offset >> 2) & 0xffffff)


def build_handoff(prefix, out):
    out.mkdir(parents=True, exist_ok=True)
    flags = ['-Os', '-Wall', '-Wextra', '-Werror', '-marm', '-march=armv5te',
             '-mfloat-abi=soft', '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
             '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-fno-pic', '-fno-pie']
    objects = []
    for name in ['handoff.S', 'handoff.c']:
        objects.append(out / (name + '.o'))
        subprocess.run([prefix + 'gcc', *flags, '-c', str(ROOT / 'boot' / name),
                        '-o', str(objects[-1])], check=True)
    elf = out / 'handoff.elf'
    subprocess.run([prefix + 'ld', '-T', str(ROOT / 'boot/handoff.ld'), *map(str, objects),
                    '-o', str(elf)], check=True)
    subprocess.run([prefix + 'objcopy', '-O', 'binary', str(elf), str(out / 'handoff.raw')],
                   check=True)
    symbols = subprocess.check_output([prefix + 'nm', '-n', str(elf)], text=True)
    assert f'{HANDOFF_PA:08x} T probe_start' in symbols, 'handoff entry must be first'
    return (out / 'handoff.raw').read_bytes()


def main():
    prefix = cross_prefix()
    stub = build_handoff(prefix, BUILD / 'handoff')
    kernel = (LINUX_OUT / 'arch/arm/boot/Image').read_bytes()
    dtb = (LINUX_OUT / 'navio505.dtb').read_bytes()
    metadata = json.loads((LINUX_OUT / 'kernel-manifest.json').read_text())
    assert sha256(LINUX_OUT / 'arch/arm/boot/Image') == metadata['image_sha256']
    assert 0 < len(kernel) < 0x1000000 and len(stub) < 0xd000 and len(dtb) < 0x10000
    assert dtb[:4] == b'\xd0\x0d\xfe\xed'
    base = entry = BASE_PA + VA
    span = KERNEL_PA - BASE_PA + len(kernel)
    span += -span % 4
    flat = bytearray(span)
    struct.pack_into('<I', flat, 0, branch(BASE_PA, HANDOFF_PA))
    struct.pack_into('<III', flat, 64, 0x43454345, base + 84, 84)        # 'ECEC', ROMHDR
    struct.pack_into('<5I', flat, 84, 0, 0, base, base + span, 1)        # ROMHDR fields
    struct.pack_into('<3I', flat, 104, base, base + span, base + span)
    struct.pack_into('<I', flat, 152, 0x000201c2)
    struct.pack_into('<I', flat, 168 + 16, base + 200)                   # TOC -> name
    flat[200:207] = b'nk.exe\0'
    for address, data in [(HANDOFF_PA, stub), (DTB_PA, dtb), (KERNEL_PA, kernel)]:
        offset = address - BASE_PA
        assert not any(flat[offset:offset + len(data)]), hex(address)
        flat[offset:offset + len(data)] = data
    assert HANDOFF_PA + len(stub) <= 0x2030e000
    assert DTB_PA + len(dtb) <= 0x20400000 <= KERNEL_PA
    image = bytearray(b'B000FF\n' + struct.pack('<II', base, span))
    records = 0
    for offset in range(0, span, RECORD):
        data = bytes(flat[offset:offset + RECORD])
        image += struct.pack('<III', base + offset, len(data), sum(data) & 0xffffffff) + data
        records += 1
    image += struct.pack('<III', 0, entry, 0)
    assert len(image) >= span, 'NAND boot reads len(file) bytes; it must cover the span'
    OUT.mkdir(exist_ok=True)
    (OUT / 'NK.nb0').write_bytes(image)
    (OUT / 'flat-image.bin').write_bytes(flat)
    subprocess.run(['python3', str(ROOT / 'scripts/check-b000ff.py'), str(OUT / 'NK.nb0')],
                   check=True)
    manifest = {
        'release': metadata['release'],
        'nk_nb0': {'bytes': len(image), 'sha256': sha256(OUT / 'NK.nb0'), 'records': records},
        'flat_image': {'bytes': len(flat), 'sha256': sha256(OUT / 'flat-image.bin')},
        'base': hex(base), 'span': hex(span), 'entry': hex(entry),
        'handoff_physical': hex(HANDOFF_PA), 'dtb_physical': hex(DTB_PA),
        'kernel_physical': hex(KERNEL_PA),
        'kernel_sha256': metadata['image_sha256'], 'dtb_sha256': metadata['dtb_sha256'],
        'handoff_sha256': sha256(BUILD / 'handoff/handoff.raw'),
        'initramfs_sha256': metadata['initramfs_sha256']}
    write_json(OUT / 'manifest.json', manifest)
    (OUT / 'SHA256SUMS').write_text(
        f"{manifest['nk_nb0']['sha256']}  NK.nb0\n{manifest['flat_image']['sha256']}  flat-image.bin\n")
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
