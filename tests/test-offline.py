#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests of the packaged image; no UART, SD card or device access.

- B000FF structure and manifest, POSIX timers (real setitimer) in the kernel
- ARM926 emulation of the handoff from the image-start branch (registers,
  CPSR, SCTLR, only UART/stack writes) and its four refusal cases
- the compiled kernel's IRQ mask/unmask/ack for all 64 lines, timer
  programming at both clock rates, counter read, stale timer IRQ
- the compiled PMU power-off sequences and the Power key poller
- optional (--eboot): the real EBOOT 0.1104 B000FF parser loads NK.nb0
  and the RAM it produces equals out/flat-image.bin

Emulation proves the code paths, not that the LCD, PMU or NAND behave.
Requires: pip install unicorn
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import BUILD, LINUX_OUT, OUT, cross_prefix, sha256  # noqa: E402

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ  # noqa: E402
from unicorn.arm_const import (UC_CPU_ARM_926, UC_ARM_REG_R0, UC_ARM_REG_R1,  # noqa: E402
    UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_CPSR,
    UC_ARM_REG_CP_REG)

META = json.loads((OUT / 'manifest.json').read_text())
PREFIX = cross_prefix()
EBOOT_0_1104_SHA256 = '49ae975404b06396b121136442e777665b2aa29bbb6d839c6639adf5d9394afd'


def emulator():
    u = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    u.ctl_set_cpu_model(UC_CPU_ARM_926)
    return u


def loader(path, expected):
    b = path.read_bytes()
    assert hashlib.sha256(b).hexdigest() == expected
    u = emulator()
    u.mem_map(0x83e00000, 0x80000)
    u.mem_write(0x83e00000, b)
    u.mem_map(0x83c00000, 0x40000)
    u.mem_write(0x83c00000, b[0x2e000:0x2e000 + 0x292b8])
    base, span = int(META['base'], 16), int(META['span'], 16)
    u.mem_map(base | 0x20000000, span + (-span % 0x1000))
    u.mem_map(0x81000000, 0x20000)
    u.reg_write(UC_ARM_REG_SP, 0x8101e000)
    u.reg_write(UC_ARM_REG_LR, 0x8101f000)
    for reg, value in zip([UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2],
                          [0x81000000, 0x81000004, 0x81000008]):
        u.reg_write(reg, value)
    image = (OUT / 'NK.nb0').read_bytes()
    position = 0

    def hook(uc, address, size, data):
        nonlocal position
        if address == 0x83e0fd20:
            count, dest = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
            assert position + count <= len(image)
            uc.mem_write(dest, image[position:position + count])
            position += count
            uc.reg_write(UC_ARM_REG_R0, 1)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        elif address in [0x83e19e60, 0x83e193a0, 0x83e0fd50]:
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x83e1a9f0, 0x8101f000, count=80000000)
    assert u.reg_read(UC_ARM_REG_PC) == 0x8101f000 and u.reg_read(UC_ARM_REG_R0) == 1
    assert struct.unpack('<3I', u.mem_read(0x81000000, 12)) == (
        int(META['base'], 16), int(META['span'], 16), int(META['entry'], 16))
    assert position == len(image)
    # The whole contiguous RAM span equals the packaged flat image; option 2
    # writes exactly this span to BINFS.
    flat = (OUT / 'flat-image.bin').read_bytes()
    assert len(flat) == span and bytes(u.mem_read(base | 0x20000000, span)) == flat
    for address, path in [(int(META['kernel_physical'], 16), LINUX_OUT / 'arch/arm/boot/Image'),
                          (int(META['handoff_physical'], 16), BUILD / 'handoff/handoff.raw'),
                          (int(META['dtb_physical'], 16), LINUX_OUT / 'navio505.dtb')]:
        content = path.read_bytes()
        assert bytes(u.mem_read(address | 0x80000000, len(content))) == content


def handoff(control=0x51078, cpsr=0x60000013, dlab=False, ready=True):
    u = emulator()
    # Physical image base 0x20300000 through the first kernel page; start at
    # the image-start branch, as EBOOT D and the NAND boot path both do.
    flat = (OUT / 'flat-image.bin').read_bytes()
    u.mem_map(0x20300000, 0x109000)
    u.mem_write(0x20300000, flat[:0x109000])
    u.mem_map(0xa0000000, 0x10000)
    u.mem_write(0xa0007618, bytes([0x83 if dlab else 3]))
    u.mem_write(0xa0007628, bytes([0x60 if ready else 0]))
    u.reg_write(UC_ARM_REG_CPSR, cpsr)
    u.reg_write(UC_ARM_REG_CP_REG, (15, 0, 0, 1, 0, 0, 0, control))
    output = bytearray()

    def guard(uc, access, address, size, value, data):
        if 0x2030e000 <= address and address + size <= 0x2030f000:
            return  # handoff stack only
        assert address == 0xa0007600 and size == 1
        output.append(value)

    u.hook_add(UC_HOOK_MEM_WRITE, guard)
    u.emu_start(0x20300000, 0x20408000, count=12000000)
    success = not (control & 5) and (cpsr & 31) == 0x13 and not dlab and ready
    if success:
        assert u.reg_read(UC_ARM_REG_PC) == 0x20408000
        assert [u.reg_read(reg) for reg in [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2]] == [0, 0xffffffff, 0x20310000]
        assert u.reg_read(UC_ARM_REG_CPSR) & 0xff == 0xd3
        value = u.reg_read(UC_ARM_REG_CP_REG, (15, 0, 0, 1, 0, 0, 0))
        assert value & 0x1005 == 0
        assert b'NAVIO LINUX HANDOFF v5' in output and output.endswith(b'WXYZJ')
        assert b'dtb_magic_word=0xedfe0dd0' in output and b'kernel_first_word=0xe92d500f' in output
    else:
        assert u.reg_read(UC_ARM_REG_PC) != 0x20408000 and not output


def callbacks():
    symbols = {}
    for line in subprocess.check_output([PREFIX + 'nm', '-n', str(LINUX_OUT / 'vmlinux')], text=True).splitlines():
        parts = line.split()
        if len(parts) == 3:
            symbols[parts[2]] = int(parts[0], 16)
    u = emulator()
    kernel = (LINUX_OUT / 'arch/arm/boot/Image').read_bytes()
    last_symbol = max(symbols[name] for name in ('intc', 'event_base', 'source_base'))
    mapped = max(0x400000, (0x8000 + len(kernel) + 0xfff) & ~0xfff,
                 (last_symbol - 0xc0000000 + 0x1000 + 0xfff) & ~0xfff)
    u.mem_map(0xc0000000, mapped)
    u.mem_write(0xc0008000, kernel)
    u.mem_map(0xa0000000, 0x10000)
    u.mem_write(symbols['intc'], struct.pack('<I', 0xa0005600))
    u.mem_write(symbols['event_base'], struct.pack('<I', 0xa00069c0))
    u.mem_write(symbols['source_base'], struct.pack('<I', 0xa00077c0))
    writes = []

    def write(uc, access, address, size, value, data):
        if address < 0xc0000000:
            assert ((size == 2 and 0xa0005600 <= address < 0xa0005640) or
                    (size == 4 and address in [0xa0005610, 0xa00069c0, 0xa00069c8, 0xa00069cc]))
            writes.append((address, value))

    u.hook_add(UC_HOOK_MEM_WRITE, write)

    def call(name, argument=0):
        writes.clear()
        u.reg_write(UC_ARM_REG_CPSR, 0xd3)
        u.reg_write(UC_ARM_REG_SP, 0xc03f0000)
        u.reg_write(UC_ARM_REG_LR, 0xc03ff000)
        u.reg_write(UC_ARM_REG_R0, argument)
        u.emu_start(symbols[name], 0xc03ff000, count=10000)
        assert u.reg_read(UC_ARM_REG_PC) == 0xc03ff000
        return list(writes)

    for irq in range(64):
        argument = 0xc03e0000
        # struct irq_data.hwirq is at offset 8 in this ARM EABI build.
        u.mem_write(argument + 8, struct.pack('<I', irq))
        address = 0xa0005600 + [0, 4, 0x30, 0x34][irq // 16]
        u.mem_write(address, b'\0\0')
        assert call('msb_mask', argument) == [(address, 1 << (irq % 16))]
        assert call('msb_unmask', argument) == [(address, 0)]
        assert call('msb_ack', argument) == ([(0xa0005610 + (irq // 16) * 4, 1 << (irq % 16))] if irq < 32 else [])
    for rate in [16369000, 24000000]:
        u.mem_write(symbols['timer_rate'], struct.pack('<I', rate))
        ticks = (rate + 50) // 100 - 1
        assert call('msb_periodic') == [(0xa00069c0, 0), (0xa00069c8, ticks & 0xffff),
                                        (0xa00069cc, ticks >> 16), (0xa00069c0, 0x101)]
        assert call('msb_shutdown') == [(0xa00069c0, 0)]
    # Model CAP latching on a low-first 32-bit read, with reserved high bits.
    reads = []
    def counter_read(uc, access, address, size, value, data):
        if address in [0xa00077d0, 0xa00077d4]:
            reads.append((address, size))
            if address == 0xa00077d0:
                uc.mem_write(0xa00077d4, struct.pack('<I', 0xabcd1234))
    u.mem_write(0xa00077d0, struct.pack('<I', 0xabcd5678))
    u.mem_write(0xa00077d4, struct.pack('<I', 0))
    u.hook_add(UC_HOOK_MEM_READ, counter_read)
    assert call('msb_clock_read') == [] and u.reg_read(UC_ARM_REG_R0) == 0x12345678
    assert reads == [(0xa00077d0, 4), (0xa00077d4, 4)]
    # A stale IRQ vector with TIMER_HIT clear must not advance Linux time.
    # clock_event_device.event_handler is its first field in this kernel.
    u.mem_write(0xc03ff100, bytes.fromhex('1eff2fe1'))  # bx lr
    u.mem_write(symbols['msb_event'], struct.pack('<I', 0xc03ff100))
    ticks_seen = []
    u.hook_add(UC_HOOK_CODE, lambda uc, addr, size, data: ticks_seen.append(addr)
               if addr == 0xc03ff100 else None)
    for hit in [0, 1]:
        u.mem_write(0xa00069c4, struct.pack('<I', hit))
        u.reg_write(UC_ARM_REG_R1, symbols['msb_event'])
        expected = [(0xa00069c0, 0), (0xa00069c0, 0x101), (0xa0005610, 2)] if hit else []
        assert call('msb_timer_irq', 16) == expected
        assert len(ticks_seen) == hit



def power():
    """Compiled Power key poller and PMU power-off sequence (kernel/arch/arm/mach-msb2521/power.c)."""
    symbols = {}
    for line in subprocess.check_output([PREFIX + 'nm', '-n', str(LINUX_OUT / 'vmlinux')], text=True).splitlines():
        parts = line.split()
        if len(parts) == 3:
            assert parts[2] not in ['pmu', 'rtc', 'chiptop', 'power_intc'] or parts[2] not in symbols
            symbols[parts[2]] = int(parts[0], 16)
    u = emulator()
    kernel = (LINUX_OUT / 'arch/arm/boot/Image').read_bytes()
    last_symbol = max(symbols[name] for name in ('pmu', 'rtc', 'chiptop', 'power_intc'))
    mapped = max(0x400000, (0x8000 + len(kernel) + 0xfff) & ~0xfff,
                 (last_symbol - 0xc0000000 + 0x1000 + 0xfff) & ~0xfff)
    u.mem_map(0xc0000000, mapped)
    u.mem_write(0xc0008000, kernel)
    u.mem_map(0xa0000000, 0x10000)
    for name, address in [('pmu', 0xa0007e00), ('rtc', 0xa0004800), ('chiptop', 0xa0003c00),
                          ('power_intc', 0xa0005600)]:
        u.mem_write(symbols[name], struct.pack('<I', address))
    writes, calls, state = [], [], {'delays': 0, 'release_after': None}
    stubs = {symbols[name]: name for name in ['_printk', '__loop_udelay', '__loop_const_udelay',
                                               '__loop_delay', 'queue_delayed_work_on',
                                               'kernel_power_off', 'cpu_arm926_do_idle']}

    def code(uc, address, size, data):
        name = stubs.get(address)
        if name is None:
            return
        if name == 'cpu_arm926_do_idle':
            calls.append(name)
            uc.emu_stop()
            return
        if name.startswith('__loop'):
            state['delays'] += 1
            if state['delays'] == state['release_after']:
                uc.mem_write(0xa0007f94, struct.pack('<I', 0))
        else:
            argument = uc.reg_read(UC_ARM_REG_R0)
            # Drop the KERN_<level> prefix (SOH + digit) of the format string.
            calls.append(bytes(uc.mem_read(argument, 80)).split(b'\0')[0][2:].decode()
                         if name == '_printk' else name)
        uc.reg_write(UC_ARM_REG_R0, 1)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def write(uc, access, address, size, value, data):
        if address < 0xc0000000:
            writes.append((address, size, value))

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_WRITE, write)

    def call(name, argument=0):
        writes.clear()
        calls.clear()
        state['delays'] = 0
        u.reg_write(UC_ARM_REG_CPSR, 0xd3)
        u.reg_write(UC_ARM_REG_SP, 0xc03f0000)
        u.reg_write(UC_ARM_REG_LR, 0xc03ff000)
        u.reg_write(UC_ARM_REG_R0, argument)
        u.emu_start(symbols[name], 0xc03ff000, count=2000000)
        return list(writes), list(calls)

    def pmu_state(key, adapter):
        u.mem_write(0xa0007f94, struct.pack('<I', 2 if key else 0))
        u.mem_write(0xa0007f84, struct.pack('<I', 0x200 if adapter else 0))
        u.mem_write(0xa0007e2c, struct.pack('<I', 0x1234))

    # CE OEMShutdown 0x8030a7e8 + cut 0x803078ac, with exact access widths.
    cut = [(0xa0005600, 2, 0xffff), (0xa0005604, 2, 0xffff), (0xa0005630, 2, 0xffff),
           (0xa0005634, 2, 0xffff), (0xa0007fc0, 4, 0xedff), (0xa0007fc4, 4, 0xffff),
           (0xa0007fd0, 4, 0xffff), (0xa0007e68, 2, 0x0ff0), (0xa0004894, 2, 0),
           (0xa0007fd0, 2, 0xffff), (0xa0007fd4, 2, 0xffff), (0xa0007e2c, 4, 0x125a)]
    # CE OEMPowerOff OFFCharging branch 0x8030ab24: sign, clear, reset.
    offcharging = [(0xa0007e68, 2, 0x00ff), (0xa0007fd0, 4, 0xffff), (0xa0007e6c, 2, 0),
                   (0xa0003c18, 2, 0x00ff), (0xa0003c18, 2, 0x0079)]
    pmu_state(key=False, adapter=False)
    seen, called = call('navio_power_off')
    assert seen == cut and called[-1] == 'cpu_arm926_do_idle'
    assert called[:-1] == ['navio505-power: PMU did not cut power, system halted\n']
    pmu_state(key=False, adapter=True)
    seen, called = call('navio_power_off')
    assert seen == offcharging + cut and called[-1] == 'cpu_arm926_do_idle'
    # Nothing is written while Power is still held; the cut follows the release.
    pmu_state(key=True, adapter=False)
    state['release_after'] = 70
    seen, called = call('navio_power_off')
    assert seen == cut and called[0] == 'navio505-power: release the Power key\n'
    state['release_after'] = None
    pmu_state(key=True, adapter=False)
    seen, called = call('navio_power_off')
    assert seen == cut and state['delays'] > 10000  # stuck key: 10 s limit, then cut

    # Poller: ignore the press that booted the device, then require 1 s.
    for name in ['key_armed', 'key_pressed']:
        u.mem_write(symbols[name], b'\0')

    def poll(key, now):
        u.mem_write(0xa0007f94, struct.pack('<I', 2 if key else 0))
        u.mem_write(symbols['jiffies'], struct.pack('<I', now & 0xffffffff))
        seen, called = call('key_poll', symbols['key_work'])
        assert seen == []
        return called

    start = 0xfffffff0  # pressed across the 32-bit jiffies wrap
    resched = 'queue_delayed_work_on'
    assert poll(True, start - 500) == [resched]
    assert poll(False, start - 400) == [resched]
    assert poll(True, start) == ['navio505-power: hold Power for 1 s to switch off\n', resched]
    assert poll(True, start + 99) == [resched]
    assert poll(False, start + 99) == ['navio505-power: Power released after %u ms, ignored\n', resched]
    assert poll(True, start + 200) == ['navio505-power: hold Power for 1 s to switch off\n', resched]
    assert poll(True, start + 300) == ['navio505-power: Power held, switching off\n', 'kernel_power_off']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--eboot', type=Path,
                        help='your own flat dump of EBOOT Columbus 0.1104 (512 KiB, NAND block 16)')
    args = parser.parse_args()
    results = []
    assert subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/check-b000ff.py'),
                           str(OUT / 'NK.nb0')], stdout=subprocess.DEVNULL).returncode == 0
    assert sha256(OUT / 'NK.nb0') == META['nk_nb0']['sha256']
    results.append('B000FF structure and manifest hash')
    config = (LINUX_OUT / '.config').read_text()
    assert 'CONFIG_POSIX_TIMERS=y' in config
    nm = subprocess.check_output([PREFIX + 'nm', str(LINUX_OUT / 'vmlinux')], text=True)
    symbols = {parts[2]: (parts[0], parts[1]) for line in nm.splitlines()
               if len(parts := line.split()) == 3}
    # ARM EABI has no alarm syscall; musl implements alarm() with setitimer,
    # so setitimer must be the real syscall, not the ENOSYS stub.
    assert symbols['sys_setitimer'][1] == 'T'
    assert symbols['sys_setitimer'][0] != symbols['sys_ni_syscall'][0]
    results.append('POSIX timers: setitimer is a real syscall (musl alarm() relies on it)')
    if args.eboot:
        loader(args.eboot, EBOOT_0_1104_SHA256)
        results.append('real EBOOT 0.1104 parser: RAM equals flat image incl. kernel/DTB/handoff')
    handoff()
    handoff(control=4)
    handoff(cpsr=0xd0)
    handoff(dlab=True)
    handoff(ready=False)
    results.append('handoff from image-start branch + 4 refusal cases')
    callbacks()
    results.append('compiled IRQ mask/unmask/ack x64, timer at 16.369/24 MHz, counter, stale IRQ')
    power()
    results.append('compiled PMU power-off (cut, OFFCharging, key release, stuck key) and Power poller')
    print(json.dumps({'status': 'PASS (offline)', 'release': META['release'],
                      'nk_nb0_sha256': META['nk_nb0']['sha256'], 'tests': results}, indent=2))


if __name__ == '__main__':
    main()
