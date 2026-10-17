// SPDX-License-Identifier: MIT
typedef unsigned int u32;
static void putc_uart(char c)
{
    volatile unsigned char *uart = (volatile unsigned char *)0xa0007600;
    u32 timeout = 1000000;
    if (uart[0x18] & 0x80) for (;;) __asm__ volatile ("nop");
    while (!(uart[0x28] & 0x20))
        if (!--timeout) for (;;) __asm__ volatile ("nop");
    uart[0] = (unsigned char)c;
}
static void text(const char *s) { while (*s) putc_uart(*s++); }
static void hex(const char *name, u32 value)
{
    static const char digits[] = "0123456789abcdef";
    text(name);
    for (int i = 28; i >= 0; i -= 4) putc_uart(digits[(value >> i) & 15]);
    text("\r\n");
}
void handoff_report(void)
{
    text("\r\nNAVIO LINUX HANDOFF v5: raw Image, image base 20300000\r\n");
    hex("clock_strap_3c2c=0x", *(volatile const unsigned short *)0xa0003c2c);
    hex("ddr_strap_7e6c=0x", *(volatile const u32 *)0xa0007e6c);
    hex("kernel_first_word=0x", *(volatile const u32 *)0x20408000);
    hex("dtb_magic_word=0x", *(volatile const u32 *)0x20310000);
    text("Linux entry=20408000 dtb=20310000 RAM=20400000..27ffffff\r\n");
}
