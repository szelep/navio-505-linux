#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate a Windows CE B000FF image the way EBOOT will read it.

Checks record checksums (byte sums modulo 2^32, not CRC), bounds, overlaps,
gaps, the end record/entry point, and the 'ECEC' + ROMHDR header.
"""
import argparse
import hashlib
from pathlib import Path
import struct
import sys


def check(path):
    data = path.read_bytes()
    errors = []
    if data[:7] != b'B000FF\n' or len(data) < 15:
        return ['not a B000FF image']
    base, span = struct.unpack_from('<II', data, 7)
    flat = bytearray(span)
    covered = bytearray(span)
    pos, records, entry = 15, 0, None
    while pos + 12 <= len(data):
        address, length, checksum = struct.unpack_from('<III', data, pos)
        pos += 12
        if address == 0 and checksum == 0:
            entry = length
            break
        payload = data[pos:pos + length]
        if not length or len(payload) != length:
            return errors + [f'truncated record {records}']
        if sum(payload) & 0xffffffff != checksum:
            errors.append(f'checksum mismatch in record {records}')
        if not (base <= address and address + length <= base + span):
            errors.append(f'record {records} outside image')
        else:
            offset = address - base
            if any(covered[offset:offset + length]):
                errors.append(f'record {records} overlaps another record')
            covered[offset:offset + length] = b'\1' * length
            flat[offset:offset + length] = payload
        pos += length
        records += 1
    if entry is None:
        errors.append('missing end record')
    elif not base <= entry < base + span:
        errors.append('entry point outside image')
    if pos != len(data):
        errors.append('trailing data after end record')
    if not all(covered):
        errors.append('image has gaps (EBOOT NAND boot needs one contiguous run)')
    if flat[0x40:0x44] != b'ECEC':
        errors.append("missing 'ECEC' signature at +0x40")
    else:
        romhdr = struct.unpack_from('<I', flat, 0x44)[0] - base
        if not 0 <= romhdr <= span - 84:
            errors.append('ROMHDR outside image')
        else:
            first, last = struct.unpack_from('<II', flat, romhdr + 8)
            if not base == first < last <= base + span:
                errors.append('ROMHDR range inconsistent with image')
    print(f'{path}: {len(data)} B sha256={hashlib.sha256(data).hexdigest()}')
    print(f'  base=0x{base:08x} span=0x{span:x} records={records} '
          f'entry=0x{entry or 0:08x} errors={len(errors)}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path, nargs='+')
    args = parser.parse_args()
    failed = False
    for image in args.images:
        for error in check(image):
            print('  ERROR:', error)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
