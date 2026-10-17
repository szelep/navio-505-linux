# Wskazówki dla agentów AI i współtwórców

Repozytorium zawiera port Linuksa 6.12 na GoClever Navio 505. Opis dla ludzi
jest w [README.md](README.md), a szczegóły techniczne
w [docs/porting-notes.md](docs/porting-notes.md). Poniższe zasady obowiązują
każdego, kto zmienia kod albo pracuje z urządzeniem, także agentów AI.

## Sprzęt

- MStar **MSB2521 U03** (ARM926EJ-S), EBOOT **Columbus 0.1104**, LCD 480×272
  RGB565, 128 MiB RAM. Nie zakładaj MediaTek MT3351, `ARCH_MSTARV7` ani
  zgodności z innymi modelami GoClever.
- Rejestry pochodzą z analizy firmware producenta (adresy funkcji
  w komentarzach). Nie zgaduj nowych rejestrów. Nowe ustalenia opisuj
  dowodem: adresem w EBOOT/CE, logiem UART albo testem.

## Budowanie i testy

```sh
make fetch && make   # out/NK.nb0, out/flat-image.bin
make test            # host + emulator ARM926; obowiązkowo przed próbą na sprzęcie
```

- `init.c` i handoff kompilują się z `-Wall -Wextra -Werror`, statycznie,
  ARMv5TE soft-float. Tak ma zostać.
- Zmiany w układzie obrazu (`scripts/package.py`, `boot/`) muszą przechodzić
  `tests/test-offline.py`. Gdy to możliwe, uruchom go też z `EBOOT=`
  z własnym zrzutem bootloadera.
- Nazwę wersji (`CONFIG_LOCALVERSION`) ustala opiekun projektu. Nie zmieniaj
  jej samodzielnie.

## Praca z urządzeniem

Każda czynność zmieniająca stan urządzenia lub karty wymaga wyraźnej zgody
właściciela urządzenia. Zgoda na jedną czynność nie obejmuje innych.

- **RESET** oznacza krótkie naciśnięcie przycisku z tyłu obudowy. Power,
  wyjęcie karty i odłączenie zasilania to inne czynności. Opisuj je osobno.
- Port UART i kartę SD identyfikuj za każdym razem na nowo
  (`/dev/serial/by-id/…`, UUID partycji). `/dev/ttyUSB*` i `/dev/sd*`
  zmieniają numery. Port może czytać tylko jeden program naraz.
- Używaj `tools/navio-uart.py` i `scripts/stage-sd.py`, a nie surowych
  terminali ani ręcznego kopiowania. Narzędzia sprawdzają menu EBOOT,
  blokują klawiaturę i robią kopie zapasowe.
- Kolejność jest zawsze ta sama: **najpierw `D` (RAM), potem ewentualnie `X`
  (NAND)**. Opcja X kasuje NAND od bloku 54, zanim wczyta plik.
  Nigdy nie wysyłaj `0`, `1`, `2`, `G`, `C` ani `R`. Opcja `2` kasuje NAND
  i niczego nie zapisuje.
- EBOOT czyta tylko FAT16 i luźno porównuje krótkie nazwy 8.3: na przykład
  `NK.original.nb0` pasuje do `NK.NB0`. Nie zostawiaj na karcie kopii pod
  kolidującymi nazwami ani pliku `UPD`. Nie formatuj karty bez zgody.
- Rozszerzenie `.nb0` nie mówi nic o formacie pliku. EBOOT przyjmuje
  B000FF, a sumy rekordów to sumy bajtów, a nie CRC.
- Komunikat SUCCESS, sam start procesu ani wynik emulatora nie potwierdzają
  działania. Sukces potwierdzają odczyt zapisanego obrazu
  (`navio-uart.py nand-verify`), log UART oraz obserwacja ekranu przez
  człowieka.

## Licencje i zawartość repozytorium

- `kernel/` jest objęty GPL-2.0-only, a reszta licencją MIT. Każdy nowy plik
  zaczyna się nagłówkiem `SPDX-License-Identifier` właściwym dla swojego
  katalogu.
- **Nie dodawaj** firmware producenta (EBOOT, NK/xip, zrzuty NAND),
  datasheetów, logów UART z numerami seryjnymi ani danych osobowych. Testy,
  które ich potrzebują, przyjmują ścieżkę od użytkownika (`--eboot`).
- Artefakty budowania (`build/`, `toolchain/`, `downloads/`, `out/`,
  `logs/`, `backups/`) są ignorowane i nie trafiają do commitów.

## Styl

- Pisz kod zgodnie z otoczeniem: kod jądra w stylu Linuksa, Python
  z biblioteką standardową (Unicorn tylko w testach). Komentarze w kodzie
  pisz po angielsku, a dokumentację po polsku.
- Commity mają zwięzłe opisy po angielsku.