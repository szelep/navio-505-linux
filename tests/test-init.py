#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host test of rootfs/init.c relay logic; no UART, SD or device access.

init.c is compiled natively and run with an LD_PRELOAD shim: /dev/ttyS0 and
/dev/console map to one host pty (the "UART"), /dev/tty1 to another pty
with fbcon's 60x17 window size (the "LCD"); mount() and getpid() are faked.
The shell is the host /bin/sh, not the ARM BusyBox. This checks the copy
loop, window size, Ctrl+C, exit/respawn and identical UART/LCD output; it
does not check the kernel, fbcon rendering or the physical LCD.
SIGUSR2 (BusyBox poweroff) is checked with kill(-1) and reboot() faked:
the host processes are never signalled and the host is never powered off.
"""
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
SHIM = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static const char *map(const char *path)
{
    if (!strcmp(path, "/dev/ttyS0") || !strcmp(path, "/dev/console")) return getenv("SHIM_UART");
    if (!strcmp(path, "/dev/tty1")) return getenv("SHIM_LCD");
    if (!strcmp(path, "/dev/fb0")) return "/dev/null";
    return path;
}
__attribute__((constructor)) static void detach(void) { unsetenv("LD_PRELOAD"); }
#define OPEN(name) int name(const char *path, int flags, ...) { \
    static int (*real)(const char *, int, ...); va_list ap; va_start(ap, flags); \
    mode_t mode = va_arg(ap, int); va_end(ap); \
    if (!real) { real = dlsym(RTLD_NEXT, #name); } return real(map(path), flags, mode); }
OPEN(open)
OPEN(open64)
int mount(const char *a, const char *b, const char *c, unsigned long d, const void *e)
{ (void)a; (void)b; (void)c; (void)d; (void)e; return 0; }
int mkdir(const char *path, mode_t mode)
{
    static int (*real)(const char *, mode_t);
    if (!strcmp(path, "/dev/pts")) return 0;
    if (!real) real = dlsym(RTLD_NEXT, "mkdir");
    return real(path, mode);
}
pid_t getpid(void) { return 1; }
static void note(const char *format, int value)
{
    FILE *log = fopen(getenv("SHIM_LOG"), "a");
    if (log) { fprintf(log, format, value); fclose(log); }
}
int kill(pid_t pid, int sig)
{
    static int (*real)(pid_t, int);
    if (pid == -1) { note("kill -1 %d\n", sig); return 0; }
    if (!real) real = dlsym(RTLD_NEXT, "kill");
    return real(pid, sig);
}
int reboot(int command) { note("reboot %#x\n", command); errno = EPERM; return -1; }
'''


class Terminal:
    def __init__(self, rows=0, cols=0):
        self.master, self.slave = os.openpty()
        if rows:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        self.name = os.ttyname(self.slave)
        self.data = bytearray()


def pump(terminals, until, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return
        ready, _, _ = select.select([t.master for t in terminals], [], [], 0.05)
        for t in terminals:
            if t.master in ready:
                try:
                    t.data += os.read(t.master, 4096)
                except OSError:
                    pass
    raise AssertionError('timeout; uart=%r lcd=%r' % (terminals[0].data[-300:], terminals[1].data[-300:]))


def main():
    work = Path(tempfile.mkdtemp(prefix='navio-init-relay-'))
    (work / 'shim.c').write_text(SHIM)
    subprocess.run(['gcc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(work / 'shim.c'), '-o', str(work / 'shim.so'), '-ldl'], check=True)
    subprocess.run(['gcc', '-O2', '-U_FORTIFY_SOURCE', '-Wall', '-Wextra', '-Werror',
                    str(ROOT / 'rootfs/init.c'), '-o', str(work / 'init')], check=True)
    uart, lcd = Terminal(), Terminal(17, 60)
    environment = dict(os.environ, LD_PRELOAD=str(work / 'shim.so'),
                       SHIM_UART=uart.name, SHIM_LCD=lcd.name, SHIM_LOG=str(work / 'shim.log'))
    process = subprocess.Popen([str(work / 'init')], env=environment, start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    both = [uart, lcd]
    prompt = b'navio# '

    def prompts(count):
        return lambda: uart.data.count(prompt) >= count and lcd.data.count(prompt) >= count

    def send(text):
        for byte in text.encode():
            os.write(uart.master, bytes([byte]))
            time.sleep(0.002)

    results = {}
    try:
        pump(both, prompts(1))
        assert b'NAVIO 505 LINUX' in lcd.data and b'LCD shell 60x17' in lcd.data
        assert b'LCD: tty1 60x17, /dev/fb0 present' in uart.data
        assert b'NAVIO 505 LINUX' not in uart.data, 'LCD-only banner leaked to UART'
        results['banner'] = 'PASS'
        send('stty size\r')
        pump(both, prompts(2))
        assert b'17 60' in uart.data and b'17 60' in lcd.data
        results['pty_window_from_tty1'] = 'PASS'
        send('echo LCD-$((6*7)) | cat\r')
        pump(both, prompts(3))
        assert b'\r\nLCD-42\r\n' in uart.data and b'\r\nLCD-42\r\n' in lcd.data
        results['pipe_output_both'] = 'PASS'
        send('sleep 30\r')
        time.sleep(0.5)
        send('\x03')
        pump(both, prompts(4))
        send('echo status=$?\r')
        pump(both, prompts(5))
        assert b'status=130' in uart.data and b'status=130' in lcd.data
        results['ctrl_c_on_pty'] = 'PASS'
        send('exit\r')
        pump(both, prompts(6))
        assert b'shell exited status=0; restarting' in uart.data
        assert b'shell exited status=0; restarting' in lcd.data
        results['exit_respawn'] = 'PASS'
        start = b'NAVIO SHELL:'
        assert uart.data[uart.data.index(start):] == lcd.data[lcd.data.index(start):]
        results['identical_shell_stream'] = 'PASS'
        assert b'Hold Power 1 s or run poweroff to switch off' in lcd.data
        process.send_signal(signal.SIGUSR2)
        failed = b'NAVIO: power off failed'
        pump(both, lambda: failed in uart.data and failed in lcd.data)
        assert uart.data.index(b'NAVIO: powering off') < uart.data.index(failed)
        assert (work / 'shim.log').read_text() == 'kill -1 15\nkill -1 9\nreboot 0x4321fedc\n'
        pump(both, prompts(7))
        results['sigusr2_poweroff'] = 'PASS'
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    print(json.dumps({'status': 'PASS host relay test; not a kernel/LCD hardware test',
                      'init_sha256': __import__('hashlib').sha256(
                          (ROOT / 'rootfs/init.c').read_bytes()).hexdigest(),
                      'results': results}, indent=2))


if __name__ == '__main__':
    sys.exit(main())
