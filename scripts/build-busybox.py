#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a static ARMv5TE soft-float BusyBox 1.37.0 with a minimal applet set."""
import subprocess

from common import BUSYBOX_OUT, BUSYBOX_SRC, cross_prefix, sha256, write_json

APPLETS = '''ash cat ls uname echo printf pwd true false sleep date uptime ps free df du
dmesg grep head tail wc sort cut sed find xargs hexdump od stat readlink realpath env
printenv mkdir rmdir rm cp mv touch tee sha256sum sync stty tty setsid kill yes mount
umount clear poweroff'''.split()
OPTIONS = '''LFS STATIC ASH SH_IS_ASH ASH_JOB_CONTROL ASH_ALIAS ASH_BUILTIN_ECHO
ASH_BUILTIN_PRINTF ASH_BUILTIN_TEST ASH_CMDCMD ASH_ECHO ASH_PRINTF ASH_TEST ASH_GETOPTS
ASH_EXPAND_PRMT FEATURE_SH_MATH FEATURE_SH_MATH_64 FEATURE_EDITING FEATURE_TAB_COMPLETION
FEATURE_EDITING_HISTORY FEATURE_PREFER_APPLETS FEATURE_SH_STANDALONE LONG_OPTS
FEATURE_LS_TIMESTAMPS FEATURE_LS_FILETYPES FEATURE_LS_SORTFILES FEATURE_LS_USERNAME
FEATURE_PS_WIDE FEATURE_PS_LONG FEATURE_FANCY_SLEEP FEATURE_DATE_ISOFMT'''.split()


def main():
    if not (BUSYBOX_SRC / 'Makefile').is_file():
        raise SystemExit(f'Missing {BUSYBOX_SRC}; run "make fetch" first')
    prefix = cross_prefix()
    BUSYBOX_OUT.mkdir(parents=True, exist_ok=True)
    command = ['make', '-C', str(BUSYBOX_SRC), 'O=' + str(BUSYBOX_OUT), 'CROSS_COMPILE=' + prefix]
    config = BUSYBOX_OUT / '.config'
    with (BUSYBOX_OUT / 'build.log').open('w') as log:
        def run(*extra, answers=False):
            subprocess.run(command + list(extra), input='\n' * 1000 if answers else None,
                           text=True, stdout=log, stderr=subprocess.STDOUT, check=True)

        run('allnoconfig', answers=True)
        enabled = set(OPTIONS + [name.upper() for name in APPLETS])
        lines = []
        for line in config.read_text().splitlines():
            name = line.removeprefix('# ').removeprefix('CONFIG_').split('=')[0].removesuffix(' is not set')
            if name in enabled:
                line = 'CONFIG_' + name + '=y'
            elif name == 'EXTRA_CFLAGS':
                line = 'CONFIG_EXTRA_CFLAGS="-march=armv5te -marm -mfloat-abi=soft"'
            elif name == 'CROSS_COMPILER_PREFIX':
                line = 'CONFIG_CROSS_COMPILER_PREFIX="' + prefix + '"'
            lines.append(line)
        config.write_text('\n'.join(lines) + '\n')
        run('oldconfig', answers=True)
        for name in ['STATIC', 'ASH', 'SH_IS_ASH', 'ASH_JOB_CONTROL', 'FEATURE_EDITING',
                     'STTY', 'POWEROFF']:
            assert 'CONFIG_' + name + '=y' in config.read_text(), name
        run('-j4')
    binary = BUSYBOX_OUT / 'busybox'
    report = subprocess.check_output([prefix + 'readelf', '-hAl', str(binary)], text=True)
    assert 'ARM' in report and 'Tag_CPU_arch: v5TE' in report
    assert 'INTERP' not in report and 'hard-float ABI' not in report, 'must be static soft-float'
    write_json(BUSYBOX_OUT / 'manifest.json', {
        'version': '1.37.0', 'binary_sha256': sha256(binary),
        'binary_bytes': binary.stat().st_size, 'config_sha256': sha256(config),
        'applets': APPLETS})
    print(f'BusyBox: {binary} ({binary.stat().st_size} B)')


if __name__ == '__main__':
    main()
