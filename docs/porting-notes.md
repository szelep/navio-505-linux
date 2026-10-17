# Notatki z portu: MSB2521 / Navio 505

Opis tego, jak działa port i skąd pochodzą adresy. Rejestry odtworzono
z działającego firmware urządzenia (EBOOT Columbus 0.1104 i jądro Windows CE
`nk.exe`), a nie z publicznej dokumentacji. Adresy funkcji w komentarzach
kodu (`0x83e…` EBOOT, `0x803…` CE) wskazują miejsca, z których je odczytano.

## Sprzęt

| Element | Wartość |
|---|---|
| SoC | MStar MSB2521 U03 („Columbus”), ARM926EJ-S, ID `0x41069265` |
| RAM | 128 MiB DDR od PA `0x20000000` |
| LCD | 480×272 RGB565, scanout ustawiony przez EBOOT |
| NAND | strony 4 KiB, 256 stron w bloku 1 MiB, 4096 bloków |
| Konsola | UART0, 16550 z `reg-shift` 3, 115200 8N1 |
| Bootloader | Windows CE EBOOT for MStar Columbus 0.1104 |

To **nie jest** MediaTek MT3351, jak podają niektóre źródła o Navio 505.
`ARCH_MSTARV7` z jądra (Cortex-A7, GIC) do tego układu nie pasuje.

## Mapa pamięci

| PA | Zawartość | Właściciel |
|---|---|---|
| `0x20000000`–`0x203fffff` | EBOOT, bufor LCD `0x20100000` (0x3fc00 B) | poza Linuksem |
| `0x20300000` | początek obrazu: skok, nagłówek `ECEC`/ROMHDR | pakiet |
| `0x20301000` | handoff (`boot/`), stos do `0x2030f000` | pakiet |
| `0x20310000` | DTB | pakiet |
| `0x20400000`–`0x27ffffff` | RAM Linuksa (124 MiB, `PHYS_OFFSET`) | jądro |
| `0x20408000` | początek `Image` | jądro |

Niskie 4 MiB nie są oddawane jądru, dlatego EBOOT może dalej odświeżać
LCD z bufora, w którym rysuje fbcon.

## Łańcuch startu

```text
RESET / Power
  └─ IPL → EBOOT 0.1104 (NAND blok 16)
       ├─ menu D: NK.nb0 z karty SD (FAT16) → RAM
       └─ start z NAND: BootConfig.ImageLength bajtów z BINFS → 0x80300000
            └─ skok na 0x20300000 (MMU i D-cache wyłączone)
                 └─ b 0x20301000: handoff
                      ├─ sprawdza tryb SVC, MMU/cache off, ID CPU
                      ├─ wypisuje stan na UART, unieważnia I-cache
                      └─ r0=0, r1=~0, r2=DTB → 0x20408000 (Linux Image)
                           └─ head.S: znaczniki A…F na UART → start_kernel
```

Znaczniki pozwalają określić, gdzie zatrzymał się start, gdy nie ma jeszcze
konsoli: `WXYZJ` wypisuje handoff, a `ABCDEF` patch `head.S`. Sekwencja
`WXYZJABCDEF`, po której pojawia się `Booting Linux`, oznacza poprawne
przekazanie sterowania.

## Format obrazu (B000FF)

EBOOT przyjmuje tylko obrazy Windows CE B000FF:

```text
"B000FF\n"  u32 ImageStart  u32 ImageLength
{ u32 adres  u32 długość  u32 suma_bajtów  dane }...
u32 0  u32 entry  u32 0
```

Suma rekordu to suma bajtów modulo 2³², a nie CRC. Dla EBOOT rozszerzenie
`.nb0` niczego nie oznacza. `scripts/package.py` zapisuje jeden ciągły zakres
od VA `0x80300000` w 62 rekordach po `0xf000` B. Nagłówek zawiera
syntetyczny ROMHDR i wpis TOC `nk.exe`, bo parser EBOOT ich wymaga.
Zakres musi być ciągły, ponieważ start z NAND czyta jednym ciągiem
`ImageLength` bajtów i skacze na początek obrazu. Ten sam plik obsługuje
więc D (RAM) oraz X (NAND).

## Co robi opcja X w menu EBOOT

Na podstawie analizy kodu EBOOT i logów instalacji:

1. kasuje każdy dobry blok od `0x36` (54, MBR) do `0xfff`, czyli system CE
   i obie jego partycje danych. Bloki 0–53 (IPL, EBOOT w bloku 16,
   BootConfig w bloku 28) pozostają nietknięte;
2. wczytuje `XIP.BIN` z karty, tworzy MBR i partycje, zapisuje obraz do BINFS
   od bloku 56;
3. zapisuje BootConfig (`LaunchAddress 0x80300000`, `ImageLength` = zakres
   obrazu) i uruchamia system.

Kasowanie następuje **przed** wczytaniem pliku. Jeśli karty nie ma lub plik
nie pasuje, NAND zostaje pusty. EBOOT nadal działa, więc wystarczy powtórzyć
X z poprawną kartą. Opcja 2 („Program NK.nb0”) w zwykłym trybie też kasuje
NAND, ale bez trybu UPD **nic nie zapisuje**. Nie używaj jej.

Obraz w BINFS jest zapisywany w jednostkach dwublokowych: strona obrazu `k`
trafia do bloku `56 + 2·(k div 512) + (k mod 2)`, strona `(k div 2) mod 256`.
Tego układu używa `navio-uart.py nand-verify`. Przy blokach uszkodzonych
w tym obszarze adresy się przesuną.

## Wybór pliku na karcie przez EBOOT

EBOOT czyta tylko FAT16 (łańcuchy FAT32 obsługuje błędnie) i porównuje
krótkie nazwy 8.3 w luźny sposób: dla zapytania `NK.NB0` pasuje każda nazwa,
której część bazowa **zaczyna się** od `NK` i która ma rozszerzenie `NB0`.
Wygrywa pierwszy wpis w kolejności katalogu.

| Plik na karcie | Krótka nazwa | Pasuje do |
|---|---|---|
| `NK.nb0` | `NK      NB0` | NK.NB0 |
| `NK.original.nb0` | `NKORIG~1NB0` | **NK.NB0 — kolizja** |
| `xip.backup.bin` | `XIPBAC~1BIN` | **XIP.BIN — kolizja** |
| `EBOOT.original.nb0` | `EBOOTO~1NB0` | **EBOOT.NB0 — kolizja** |
| `UPD` | `UPD` | tryb aktualizacji EBOOT |

Reguła w `scripts/stage-sd.py` (`eboot_matches`) była porównana
z wykonaniem oryginalnej funkcji EBOOT (`0x83e26c80`) w emulatorze na
24 000 losowych i brzegowych nazwach, bez żadnej rozbieżności.
Kopii zapasowych nie trzymaj na karcie pod nazwami, które mogą kolidować.

## Sterowniki w `kernel/arch/arm/mach-msb2521`

**`board.c`**

- Kontroler przerwań `0xa0005600`, 64 linie, maski `+0x00/+0x04/+0x30/+0x34`.
  Linie 0–31 (bank „FIQ” obsługiwany w CE jako IRQ) mają zatrzask
  kasowany zapisem 1 (`+0x10/+0x14`), a linie 32–63 są poziomowe.
  Numer wektora odczytywany jest z `+0x88` lub `+0x8c`.
- Timer0 `0xa00069c0` to periodyczny clockevent (IRQ 1, HZ=100). Timer2
  `0xa00077c0` to 32-bitowy clocksource i `sched_clock`. Licznik zatrzaskuje
  się przy odczycie młodszego słowa.
- Zegar timerów i UART: 16,369 MHz albo 24 MHz zależnie od bitu 6 strapu
  `0xa0003c2c`. Wartość trafia do `clock-frequency` UART w DT przed
  utworzeniem urządzeń. Port nie przeprogramowuje PLL.
- `lpj=1600000` w bootargs pomija kalibrację pętli opóźnień.

**`power.c`** (PMU `0xa0007e00`)

- `+0x194` bit 1 to stan przycisku Power, a `+0x184` bit 9 obecność
  zasilacza/USB. Przycisk jest odpytywany co 50 ms. Przytrzymanie przez
  1 s wywołuje `kernel_power_off()`. Wciśnięcie, którym włączono urządzenie,
  jest ignorowane do pierwszego puszczenia.
- Wyłączenie odtwarza sekwencje CE. Bez zasilacza: maski wybudzenia, znak
  startu `0x0ff0`, filtr RTC 0 i `0x5a` do `+0x2c`, co odcina zasilanie.
  Z zasilaczem: znak `0x00ff` i reset układu, po którym EBOOT przechodzi
  w tryb ładowania OFFCharging.

**`navio505.dts`**

- `simple-framebuffer` na buforze EBOOT, `ns16550a` na UART0, bez IRQ
  (sterownik 8250 odpytuje RX co 10 ms, więc wklejany tekst trzeba wysyłać
  porcjami; `navio-uart.py` robi to sam).
- Konsola jądra na `tty1` (LCD) i `ttyS0`, `rdinit=/init`, `panic=0`.

## Przestrzeń użytkownika

`rootfs/init.c` to PID 1. Montuje devtmpfs, proc, sysfs i devpts, a potem
uruchamia BusyBox `ash` na pseudoterminalu z rozmiarem okna LCD (60×17).
Bajty z UART trafiają do powłoki, a jej wyjście jest kopiowane na UART i LCD.
Po wyjściu z powłoki init uruchamia ją ponownie. SIGUSR2, czyli BusyBox
`poweroff`, kończy procesy, wykonuje `sync` i `reboot(RB_POWER_OFF)`.
Initramfs zawiera tylko `init`, statyczny BusyBox 1.37.0 (musl, ARMv5TE
soft-float) i dowiązania apletów.
