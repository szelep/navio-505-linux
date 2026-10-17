#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Configure and build Linux 6.12 for the Navio 505, plus its device tree.

The board directory kernel/arch/arm/mach-msb2521 is copied into the patched
tree on every run, so edits in this repository are always what gets built.
"""
import argparse
import os
import shutil
import subprocess

from common import INITRAMFS_OUT, LINUX_OUT, LINUX_SRC, ROOT, cross_prefix, sha256, write_json

KERNEL = ROOT / 'kernel'
REQUIRED = ['CONFIG_CPU_ARM926T=y', 'CONFIG_ARCH_MSB2521=y', 'CONFIG_AEABI=y', 'CONFIG_OF=y',
            'CONFIG_PHYS_OFFSET=0x20400000', 'CONFIG_SERIAL_8250_CONSOLE=y',
            'CONFIG_HZ_PERIODIC=y', 'CONFIG_POSIX_TIMERS=y',
            # LCD: EBOOT scanout as simple-framebuffer with fbcon on tty1.
            'CONFIG_FB_SIMPLE=y', 'CONFIG_FRAMEBUFFER_CONSOLE=y', 'CONFIG_VT_CONSOLE=y',
            'CONFIG_FONT_8x16=y', 'CONFIG_FB_DEVICE=y', 'CONFIG_UNIX98_PTYS=y']
FORBIDDEN = ['CONFIG_ARCH_MSTARV7=y', 'CONFIG_CPU_V7=y', 'CONFIG_THUMB2_KERNEL=y',
             'CONFIG_MTD=y', 'CONFIG_SMP=y',
             # simpledrm would also claim simple-framebuffer; deferred takeover hides fbcon.
             'CONFIG_DRM_SIMPLEDRM=y', 'CONFIG_FRAMEBUFFER_CONSOLE_DEFERRED_TAKEOVER=y']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=os.cpu_count() or 4)
    parser.add_argument('--configure-only', action='store_true')
    args = parser.parse_args()
    if not (LINUX_SRC / 'Makefile').is_file():
        raise SystemExit(f'Missing {LINUX_SRC}; run "make fetch" first')
    initramfs = INITRAMFS_OUT / 'initramfs.cpio'
    if not initramfs.is_file():
        raise SystemExit(f'Missing {initramfs}; run "make initramfs" first')
    board = LINUX_SRC / 'arch/arm/mach-msb2521'
    shutil.rmtree(board, ignore_errors=True)
    shutil.copytree(KERNEL / 'arch/arm/mach-msb2521', board)
    LINUX_OUT.mkdir(parents=True, exist_ok=True)
    # Fixed build identity: /proc/version must not carry the builder's login.
    env = dict(os.environ, KBUILD_BUILD_USER='navio505', KBUILD_BUILD_HOST='linux')
    make = ['make', '-C', str(LINUX_SRC), 'O=' + str(LINUX_OUT), 'ARCH=arm',
            'CROSS_COMPILE=' + cross_prefix()]
    subprocess.run(make + ['KCONFIG_ALLCONFIG=' + str(KERNEL / 'navio505_defconfig'),
                           'allnoconfig'], check=True, env=env)
    subprocess.run([str(LINUX_SRC / 'scripts/config'), '--file', str(LINUX_OUT / '.config'),
                    '--set-str', 'INITRAMFS_SOURCE', str(initramfs)], check=True)
    subprocess.run(make + ['olddefconfig'], check=True, env=env)
    config = (LINUX_OUT / '.config').read_text()
    for option in REQUIRED:
        assert option in config, f'{option} missing from .config'
    for option in FORBIDDEN:
        assert option not in config, f'{option} must not be enabled'
    if args.configure_only:
        return
    subprocess.run(make + [f'-j{args.jobs}', 'Image', 'scripts_dtc'], check=True, env=env)
    dtb = LINUX_OUT / 'navio505.dtb'
    subprocess.run([str(LINUX_OUT / 'scripts/dtc/dtc'), '-I', 'dts', '-O', 'dtb',
                    '-o', str(dtb), str(KERNEL / 'navio505.dts')], check=True)
    image = LINUX_OUT / 'arch/arm/boot/Image'
    release = (LINUX_OUT / 'include/config/kernel.release').read_text().strip()
    write_json(LINUX_OUT / 'kernel-manifest.json', {
        'release': release, 'kernel_physical_entry': '0x20408000',
        'linux_ram': ['0x20400000', '0x28000000'],
        'image_bytes': image.stat().st_size, 'image_sha256': sha256(image),
        'dtb_sha256': sha256(dtb), 'config_sha256': sha256(LINUX_OUT / '.config'),
        'initramfs_sha256': sha256(initramfs)})
    print(f'Linux {release}: {image} ({image.stat().st_size} B), {dtb}')


if __name__ == '__main__':
    main()
