/* SPDX-License-Identifier: MIT */
/* Navio 505 EBOOT handoff, executed in physical RAM at 0x20301000.
 * Reached through the branch at image start 0x20300000: EBOOT D (LaunchAddr)
 * and the NAND boot path (BootConfig LaunchAddress 0x80300000) both jump
 * there. No NAND, PLL or DRAM register writes. */
.syntax unified
.arm
.section .startup,"ax"
.macro handoff_marker, ch
    ldr r3, =0xa0007600
    mov r2, #\ch
1001:
    ldrb r1, [r3, #0x28]
    tst r1, #0x20
    beq 1001b
    strb r2, [r3]
.endm
.global probe_start
probe_start:
    mrs r4, cpsr
    and r0, r4, #0x1f
    cmp r0, #0x13
    bne handoff_refuse
    mrc p15, 0, r6, c1, c0, 0
    tst r6, #5
    bne handoff_refuse
    orr r0, r4, #0xc0
    msr cpsr_c, r0
    mrc p15, 0, r0, c0, c0, 0
    ldr r1, =0x41069265
    cmp r0, r1
    bne handoff_refuse
    ldr sp, =0x2030f000
    bl handoff_report
    handoff_marker 87          /* W: C reporter returned */
    /* EBOOT has already cleaned and disabled D-cache/MMU. Invalidate the
     * old instruction cache, disable I-cache and drain the write buffer
     * before the raw Linux Image's first instruction. */
    mov r0, #0
    mcr p15, 0, r0, c7, c10, 4
    handoff_marker 88          /* X: write-buffer drain completed */
    mcr p15, 0, r0, c7, c5, 0
    handoff_marker 89          /* Y: I-cache invalidation completed */
    mrc p15, 0, r0, c1, c0, 0
    bic r0, r0, #0x1000
    mcr p15, 0, r0, c1, c0, 0
    handoff_marker 90          /* Z: I-cache disabled */
    /* ram3 reached Z but not J around the prefetch-flush instruction.
     * Omit that extra operation; retain drained buffers and disabled caches. */
    handoff_marker 74          /* J: cache handoff complete, about to jump */
    mov r0, #0
    mvn r1, #0
    ldr r2, =0x20310000
    ldr pc, =0x20408000
.global handoff_refuse
handoff_refuse:
    b handoff_refuse
.ltorg
.section .note.GNU-stack,"",%progbits
