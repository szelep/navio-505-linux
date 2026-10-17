// SPDX-License-Identifier: MIT
/* RAM-only PID 1: mount virtual filesystems and supervise the shell.
 * v4: the shell runs on a pseudo-terminal. Keystrokes come from ttyS0;
 * every byte the shell writes is copied to ttyS0 and to the LCD (tty1).
 * v5: SIGUSR2 (BusyBox poweroff) powers the device off. The Power key
 * is handled by the kernel (kernel/arch/arm/mach-msb2521/power.c).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

static volatile sig_atomic_t poweroff_requested;

static void request_poweroff(int number)
{
    (void)number;
    poweroff_requested = 1;
}

static void fatal(const char *what)
{
    perror(what);
    for (;;) pause();
}

/* Raw byte path: line discipline, echo and ^C belong to the pty slave. */
static int open_raw(const char *path, int flags)
{
    struct termios tty;
    int fd = open(path, flags | O_NOCTTY);
    if (fd < 0) return -1;
    if (tcgetattr(fd, &tty) == 0) {
        cfmakeraw(&tty);
        tty.c_cflag |= CLOCAL | CREAD;
        tty.c_cc[VMIN] = 1;
        tty.c_cc[VTIME] = 0;
        if (!strcmp(path, "/dev/ttyS0")) {
            cfsetispeed(&tty, B115200);
            cfsetospeed(&tty, B115200);
        }
        if (tcsetattr(fd, TCSANOW, &tty) < 0) perror(path);
    }
    return fd;
}

static void write_all(int fd, const char *data, size_t length)
{
    while (fd >= 0 && length) {
        ssize_t n = write(fd, data, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return;
        data += n;
        length -= (size_t)n;
    }
}

static void say(int uart, int lcd, const char *text)
{
    write_all(uart, text, strlen(text));
    write_all(lcd, text, strlen(text));
}

static void shell(const char *slave)
{
    struct termios tty;
    if (setsid() < 0) { perror("setsid"); _exit(1); }
    int fd = open(slave, O_RDWR);
    if (fd < 0) { perror("open pty slave"); _exit(1); }
    if (ioctl(fd, TIOCSCTTY, 0) < 0) { perror("TIOCSCTTY"); _exit(1); }
    if (tcgetattr(fd, &tty) < 0) { perror("tcgetattr"); _exit(1); }
    tty.c_iflag = ICRNL;
    tty.c_oflag = OPOST | ONLCR;
    tty.c_cflag = CS8 | CREAD | CLOCAL;
    tty.c_lflag = ISIG | ICANON | ECHO | ECHOE | ECHOK;
    tty.c_cc[VINTR] = 3;
    tty.c_cc[VQUIT] = 28;
    tty.c_cc[VERASE] = 127;
    tty.c_cc[VKILL] = 21;
    tty.c_cc[VEOF] = 4;
    tty.c_cc[VSUSP] = 26;
    tty.c_cc[VMIN] = 1;
    tty.c_cc[VTIME] = 0;
    if (tcsetattr(fd, TCSANOW, &tty) < 0) { perror("tcsetattr"); _exit(1); }
    if (tcsetpgrp(fd, getpgrp()) < 0) { perror("tcsetpgrp"); _exit(1); }
    for (int n = 0; n < 3; n++)
        if (dup2(fd, n) < 0) { perror("dup2 pty"); _exit(1); }
    if (fd > 2) close(fd);
    chdir("/root");
    setenv("PATH", "/bin", 1);
    setenv("HOME", "/root", 1);
    setenv("TERM", "linux", 1);
    setenv("PS1", "navio# ", 1);
    setenv("PS2", "> ", 1);
    puts("NAVIO SHELL: BusyBox ash; input UART, output UART + LCD; exit respawns.");
    execl("/bin/sh", "sh", "-i", (char *)NULL);
    perror("exec /bin/sh");
    _exit(1);
}

static int open_pty(char *slave, size_t size, const struct winsize *window)
{
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0) return -1;
    if (grantpt(master) || unlockpt(master) || ptsname_r(master, slave, size)) {
        close(master);
        return -1;
    }
    if (window->ws_row && ioctl(master, TIOCSWINSZ, window) < 0) perror("TIOCSWINSZ");
    fcntl(master, F_SETFD, FD_CLOEXEC);
    return master;
}

/* Copy until the shell is reaped; reap every orphan as PID 1. */
static int relay(int uart, int lcd, int master, pid_t child)
{
    char buffer[512];
    int status = 0, alive = 1, master_open = 1;
    while ((alive || master_open) && !poweroff_requested) {
        struct pollfd fds[2] = {
            { .fd = uart, .events = POLLIN },
            { .fd = master_open ? master : -1, .events = POLLIN },
        };
        int ready = poll(fds, 2, alive ? 250 : 50);
        if (ready < 0 && errno != EINTR) { perror("poll"); sleep(1); }
        if (ready > 0 && (fds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
            ssize_t n = read(master, buffer, sizeof(buffer));
            if (n > 0) {
                write_all(uart, buffer, (size_t)n);
                write_all(lcd, buffer, (size_t)n);
            } else if (n == 0 || errno != EINTR) {
                /* EIO: every slave descriptor is closed. */
                master_open = 0;
            }
        }
        if (ready > 0 && (fds[0].revents & POLLIN)) {
            ssize_t n = read(uart, buffer, sizeof(buffer));
            if (n > 0 && alive && master_open) write_all(master, buffer, (size_t)n);
        }
        pid_t reaped;
        int code;
        while ((reaped = waitpid(-1, &code, WNOHANG)) > 0)
            if (reaped == child) { alive = 0; status = code; }
        /* Once the shell is gone, drain what is already queued and stop. */
        if (!alive && master_open && ready == 0) master_open = 0;
    }
    return status;
}

static void power_off(int uart, int lcd)
{
    say(uart, lcd, "\r\nNAVIO: powering off\r\n");
    kill(-1, SIGTERM);
    sleep(1);
    kill(-1, SIGKILL);
    sync();
    reboot(RB_POWER_OFF);
    say(uart, lcd, "NAVIO: power off failed\r\n");
    poweroff_requested = 0;
}

int main(void)
{
    struct utsname info;
    struct winsize window = { 0 };
    char slave[64], line[256];
    int console = open("/dev/console", O_RDWR);
    if (console >= 0) {
        for (int n = 0; n < 3; n++) (void)dup2(console, n);
        if (console > 2) close(console);
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    puts("NAVIO LINUX INITRAMFS v5: shell on pty, UART input, UART + LCD output; RAM only");
    if (getpid() != 1) fatal("init requires PID 1");
    if (uname(&info) == 0) printf("kernel=%s release=%s machine=%s\n", info.sysname, info.release, info.machine);
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) && errno != EBUSY) fatal("mount devtmpfs");
    if (mount("proc", "/proc", "proc", 0, NULL)) fatal("mount proc");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL)) fatal("mount sysfs");
    if (mkdir("/dev/pts", 0755) && errno != EEXIST) fatal("mkdir /dev/pts");
    if (mount("devpts", "/dev/pts", "devpts", 0, "mode=0620,ptmxmode=0666")) fatal("mount devpts");
    struct sigaction action = { .sa_handler = request_poweroff };
    if (sigaction(SIGUSR2, &action, NULL)) perror("sigaction SIGUSR2");

    int lcd = open_raw("/dev/tty1", O_WRONLY);
    if (lcd < 0) {
        printf("LCD: /dev/tty1 unavailable: %s; UART only\n", strerror(errno));
    } else if (ioctl(lcd, TIOCGWINSZ, &window) < 0 || !window.ws_row) {
        puts("LCD: tty1 window size unknown");
        memset(&window, 0, sizeof(window));
    }
    if (lcd >= 0) {
        int fb = open("/dev/fb0", O_RDONLY);
        printf("LCD: tty1 %ux%u, /dev/fb0 %s\n", window.ws_col, window.ws_row,
               fb >= 0 ? "present" : strerror(errno));
        if (fb >= 0) close(fb);
        say(-1, lcd, "\033[H\033[J");
        snprintf(line, sizeof(line), "NAVIO 505 LINUX %s\r\nLCD shell %ux%u; type on UART 115200 8N1\r\n"
                 "Hold Power 1 s or run poweroff to switch off\r\n\r\n",
                 info.release, window.ws_col, window.ws_row);
        say(-1, lcd, line);
    }
    /* After this point ttyS0 is raw: messages need explicit CR LF. */
    int uart = open_raw("/dev/ttyS0", O_RDWR);
    if (uart < 0) fatal("open ttyS0");
    for (;;) {
        int master = open_pty(slave, sizeof(slave), &window);
        if (master < 0) { perror("open pty"); sleep(5); continue; }
        pid_t child = fork();
        if (child == 0) {
            close(uart);
            if (lcd >= 0) close(lcd);
            close(master);
            shell(slave);
        }
        if (child < 0) { perror("fork shell"); close(master); sleep(5); continue; }
        int status = relay(uart, lcd, master, child);
        close(master);
        if (poweroff_requested) {
            power_off(uart, lcd);
            continue;
        }
        snprintf(line, sizeof(line), "\r\nshell exited status=%d; restarting\r\n", status);
        say(uart, lcd, line);
        sleep(1);
    }
}
