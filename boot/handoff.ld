/* SPDX-License-Identifier: MIT */
ENTRY(probe_start)
SECTIONS
{
    /* Image base 0x20300000 holds the B000FF/ROMHDR header and a branch here. */
    . = 0x20301000;
    .text : { *(.startup) *(.text*) *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }
    ASSERT(SIZEOF(.bss) == 0, "handoff must not require BSS initialization")
    ASSERT(. < 0x2030e000, "handoff overlaps its stack below 0x2030f000")
    /DISCARD/ : { *(.comment*) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
