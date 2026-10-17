#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Talk to the Navio 505 over its UART0 (USB-UART adapter, 115200 8N1).

  monitor       print and log everything, send nothing
  menu          stop the EBOOT countdown after RESET and wait at its menu
  ram-boot      menu, then D: load NK.nb0 from SD into RAM and run it
  nand-install  menu, then X: ERASE NAND from block 54 and install XIP.BIN
  shell         interactive Linux shell (Ctrl-] quits), or -c COMMAND
  nand-verify   read pages back with EBOOT B and compare with flat-image.bin

Every received byte is logged to logs/uart-<time>.bin. The port is opened
exclusively (TIOCEXCL), so a second program cannot read it at the same time.
Menu keys are sent only after the full EBOOT menu has been seen. The shell
starts locked and only passes CR and Ctrl-C (both ignored by the EBOOT menu)
until a fresh navio# prompt arrives; any EBOOT or boot output locks it again,
so typing can never select an EBOOT menu option.
"""
import argparse
import datetime
import fcntl
import hashlib
import os
from pathlib import Path
import re
import select
import sys
import termios
import time
import tty

ROOT = Path(__file__).resolve().parents[1]
COUNTDOWN = b'Press [ENTER] to download now or [SPACE]'
MENU_TITLE = b'MStar EBOOT Configuration'
MENU_PROMPT = b'Enter your selection: '
BLOCK_PROMPT = b'Enter NAND flash block number (in decimal): '
PAGE_PROMPT = b'Enter NAND flash page number (in decimal): '
BOOT_SIGNS = [COUNTDOWN, MENU_TITLE, b'Microsoft Windows CE Bootloader',
              b'NAVIO LINUX HANDOFF', b'Booting Linux']
SHELL_PROMPT = b'navio# '
PAGE = 4096


class Port:
    def __init__(self, path, echo=True):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
        except OSError:
            pass
        self.saved = termios.tcgetattr(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        logs = ROOT / 'logs'
        logs.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.log_path = logs / f'uart-{stamp}.bin'
        self.log = self.log_path.open('ab')
        self.recent = b''
        self.total = 0
        self.echo = echo
        print(f'[{path} 115200 8N1, log {self.log_path.relative_to(ROOT)}]', file=sys.stderr)

    def close(self):
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        except termios.error:
            pass  # adapter unplugged
        os.close(self.fd)
        self.log.close()

    def read(self, timeout):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b''
        data = os.read(self.fd, 16384)
        if not data:
            raise SystemExit('UART adapter disconnected')
        self.log.write(data)
        self.log.flush()
        self.recent = (self.recent + data)[-262144:]
        self.total += len(data)
        if self.echo:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        return data

    def write(self, data, chunk=8, gap=0.02):
        # The Linux 8250 driver polls RX every 10 ms (HZ=100, no RX IRQ) and
        # the FIFO holds 16 bytes: pace writes so pasted text is not lost.
        for start in range(0, len(data), chunk):
            os.write(self.fd, data[start:start + chunk])
            if start + chunk < len(data):
                time.sleep(gap)

    def forget(self):
        self.recent = b''

    def wait_for(self, token, timeout):
        deadline = time.monotonic() + timeout
        while token not in self.recent:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'did not see {token.decode()!r} within {timeout:.0f} s')
            self.read(min(left, 0.2))


def stop_at_menu(port, timeout):
    print('\n>>> Press RESET on the back of the Navio now (short press). '
          'Waiting for the EBOOT countdown...', file=sys.stderr)
    port.forget()
    port.wait_for(COUNTDOWN, timeout)
    # The boot delay is one second: keep sending SPACE until the menu shows.
    stop = time.monotonic() + 0.8
    next_space = 0
    while MENU_TITLE not in port.recent and time.monotonic() < stop:
        if time.monotonic() >= next_space:
            os.write(port.fd, b' ')
            next_space = time.monotonic() + 0.08
        port.read(0.02)
    try:
        port.wait_for(MENU_TITLE, 5)
        port.wait_for(MENU_PROMPT, 5)
    except TimeoutError:
        raise SystemExit('\nMissed the EBOOT menu (the stored system is probably booting). '
                         'Run the command again and press RESET.')
    print('\n>>> At the EBOOT menu.', file=sys.stderr)


def at_menu(port, args):
    if args.at_menu:
        print('>>> Assuming the device already shows the EBOOT menu (--at-menu).',
              file=sys.stderr)
    else:
        stop_at_menu(port, args.timeout)


def cmd_monitor(port, args):
    deadline = time.monotonic() + args.seconds if args.seconds else None
    while deadline is None or time.monotonic() < deadline:
        port.read(0.5)


def cmd_menu(port, args):
    stop_at_menu(port, args.timeout)


def cmd_ram_boot(port, args):
    at_menu(port, args)
    port.forget()
    os.write(port.fd, b'D')
    print('\n>>> Sent D: EBOOT loads NK.nb0 from the SD card into RAM. NAND is not written.',
          file=sys.stderr)
    port.wait_for(SHELL_PROMPT, args.boot_timeout)
    print('\n>>> Linux shell is up (RAM). Use "navio-uart.py shell". '
          'RESET returns to the system stored in NAND.', file=sys.stderr)


def cmd_nand_install(port, args):
    if not args.erase_nand:
        raise SystemExit('nand-install erases the NAND from block 54 (Windows CE system and '
                         'its data partitions) and writes XIP.BIN from the SD card.\n'
                         'Re-run with --erase-nand once the RAM test passed and the card '
                         'was staged with "stage-sd.py nand".')
    at_menu(port, args)
    if sys.stdin.isatty():
        answer = input('\nType ERASE to send X and install XIP.BIN to NAND: ')
        if answer.strip() != 'ERASE':
            raise SystemExit('Nothing sent.')
    port.forget()
    os.write(port.fd, b'X')
    print('\n>>> Sent X. Do not press RESET or remove the card until the new system runs.',
          file=sys.stderr)
    if args.no_wait:
        print('>>> Monitoring; press Ctrl-C once the installed system has started.',
              file=sys.stderr)
        while True:
            port.read(0.5)
    port.wait_for(SHELL_PROMPT, args.boot_timeout)
    print('\n>>> Installed and booted from NAND. Verify with "navio-uart.py nand-verify" '
          'and a RESET without the SD card.', file=sys.stderr)


class Gate:
    """Keyboard gate for the shell. Locked, only CR and Ctrl-C pass: the EBOOT
    menu ignores both. It unlocks on a fresh navio# prompt and locks again as
    soon as EBOOT or a new Linux boot shows up on the line."""

    def __init__(self, port):
        self.port, self.locked, self.seen = port, True, port.total

    def update(self):
        recent, new = self.port.recent, self.port.total - self.seen
        self.seen = self.port.total
        fresh = recent[-(new + 64):] if new else b''
        if any(sign in fresh for sign in BOOT_SIGNS):
            if not self.locked:
                sys.stderr.write('\r\n[boot output seen: keyboard locked until navio# '
                                 'appears]\r\n')
            self.locked = True
            self.port.forget()
        elif self.locked and recent.endswith(SHELL_PROMPT):
            self.locked = False
            return True
        return False

    def send(self, data):
        for start in range(0, len(data), 8):
            chunk = data[start:start + 8]
            self.update()
            if self.locked and not set(chunk) <= {0x0d, 0x03}:
                return False
            os.write(self.port.fd, chunk)
            self.port.read(0.02)
        return True


def probe_prompt(port, gate, timeout=3):
    gate.update()
    if gate.locked:
        port.forget()
    os.write(port.fd, b'\r')
    deadline = time.monotonic() + timeout
    while gate.locked and time.monotonic() < deadline:
        port.read(0.1)
        gate.update()
    return not gate.locked


def cmd_shell(port, args):
    gate = Gate(port)
    if args.command:
        port.echo = False
        if not probe_prompt(port, gate):
            raise SystemExit('No Linux navio# prompt on the line; only CR was sent.')
        for command in args.command:
            port.forget()
            if not gate.send(command.encode() + b'\r'):
                raise SystemExit('Boot output appeared while sending; stopped.')
            deadline = time.monotonic() + args.timeout
            while not port.recent.endswith(b'\n' + SHELL_PROMPT):
                if time.monotonic() > deadline:
                    gate.send(b'\x03')
                    raise SystemExit(f'Timeout in {command!r}; sent Ctrl-C.')
                port.read(0.1)
                gate.update()
                if gate.locked:
                    raise SystemExit(f'Device rebooted during {command!r}.')
            text = port.recent.decode('utf-8', 'replace').replace('\r', '')
            body = text.split('\n', 1)[1] if '\n' in text else ''
            print(body[:-len(SHELL_PROMPT)], end='')
        return
    print('[Linux shell; Ctrl-] quits. Keyboard is locked until a navio# prompt is seen.]',
          file=sys.stderr)
    if probe_prompt(port, gate):
        sys.stderr.write('\r\n[Linux shell found: keyboard enabled]\r\n')
    stdin = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin)
    tty.setraw(stdin)
    try:
        while True:
            ready, _, _ = select.select([port.fd, stdin], [], [], 0.5)
            if port.fd in ready:
                port.read(0)
                if gate.update():
                    sys.stderr.write('\r\n[Linux shell found: keyboard enabled]\r\n')
            if stdin in ready:
                data = os.read(stdin, 1024)
                if b'\x1d' in data:
                    break
                gate.send(data)
    finally:
        termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        print(file=sys.stderr)


def nand_location(index):
    """Image page -> (block, page): BINFS starts at block 56 and EBOOT writes
    two-block multi-plane units, alternating blocks page by page."""
    return 56 + 2 * (index // 512) + index % 2, (index // 2) % 256


def read_nand_page(port, block, page):
    port.forget()
    os.write(port.fd, b'B')
    port.wait_for(BLOCK_PROMPT, 30)
    os.write(port.fd, str(block).encode() + b'\r')
    port.wait_for(PAGE_PROMPT, 30)
    port.forget()
    os.write(port.fd, str(page).encode() + b'\r')
    port.wait_for(MENU_PROMPT, 60)
    header = f'===== Data dump of page(0x{page:X}) / block(0x{block:X}) ====='.encode().lower()
    data = port.recent.lower()
    if header not in data:
        raise ValueError(f'unexpected dump header for block {block} page {page}')
    body = data.split(header, 1)[1].split(b'si dump', 1)[0]
    rows = re.findall(rb'0x([0-9a-f]{8}): ((?:[0-9a-f]{2} ){15}[0-9a-f]{2})', body)
    if [int(offset, 16) for offset, _ in rows] != list(range(0, PAGE, 16)):
        raise ValueError(f'incomplete dump of block {block} page {page}')
    return b''.join(bytes.fromhex(values.decode()) for _, values in rows)


def cmd_nand_verify(port, args):
    flat = args.image.read_bytes()
    total = (len(flat) + PAGE - 1) // PAGE
    if args.all:
        pages = list(range(total))
    else:
        pages = sorted({0, 1, 2, 3, total - 2, total - 1, *range(0, total, 64)})
    print(f'{args.image}: {total} pages, checking {len(pages)} '
          f'(about {len(pages) * 16 // 60 + 1} min)', file=sys.stderr)
    at_menu(port, args)
    port.echo = False
    bad = 0
    for count, index in enumerate(pages, 1):
        block, page = nand_location(index)
        data = read_nand_page(port, block, page)
        want = flat[index * PAGE:(index + 1) * PAGE]
        same = data[:len(want)] == want
        bad += not same
        print(f'[{count}/{len(pages)}] image page {index} = block {block} page {page}: '
              f'{"identical" if same else "DIFFERENT"}  sha256 {hashlib.sha256(data).hexdigest()[:16]}',
              file=sys.stderr)
    if bad:
        raise SystemExit(f'{bad} page(s) differ. Do not repeat X blindly: check the card '
                         'contents and bad-block remapping first.')
    print('PASS: all checked pages are identical to the image.', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', required=True,
                        help='serial device, preferably /dev/serial/by-id/usb-...')
    sub = parser.add_subparsers(dest='command_name', required=True)
    p = sub.add_parser('monitor', help='receive only')
    p.add_argument('--seconds', type=float, default=0, help='stop after N seconds (0 = never)')
    p.set_defaults(func=cmd_monitor)
    for name, func, boot in [('menu', cmd_menu, None), ('ram-boot', cmd_ram_boot, 180),
                             ('nand-install', cmd_nand_install, 900),
                             ('nand-verify', cmd_nand_verify, None)]:
        p = sub.add_parser(name)
        p.add_argument('--timeout', type=float, default=300,
                       help='seconds to wait for RESET / the EBOOT countdown')
        if name != 'menu':
            p.add_argument('--at-menu', action='store_true',
                           help='the device already shows the EBOOT menu; skip RESET')
        if boot:
            p.add_argument('--boot-timeout', type=float, default=boot)
        p.set_defaults(func=func)
    sub.choices['nand-install'].add_argument('--erase-nand', action='store_true',
                                             help='confirm that NAND from block 54 is erased')
    sub.choices['nand-install'].add_argument('--no-wait', action='store_true',
                                             help='do not wait for navio# (e.g. reinstalling '
                                                  'Windows CE); monitor until Ctrl-C')
    sub.choices['nand-verify'].add_argument('--image', type=Path,
                                            default=ROOT / 'out/flat-image.bin')
    sub.choices['nand-verify'].add_argument('--all', action='store_true',
                                            help='every page (about 16 s each)')
    p = sub.add_parser('shell', help='Linux shell on UART')
    p.add_argument('-c', dest='command', action='append', help='run COMMAND and print output')
    p.add_argument('--timeout', type=float, default=30)
    p.set_defaults(func=cmd_shell)
    args = parser.parse_args()
    port = Port(args.port)
    try:
        args.func(port, args)
    except TimeoutError as error:
        raise SystemExit(f'\nTimeout: {error}')
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
    finally:
        port.close()


if __name__ == '__main__':
    main()
