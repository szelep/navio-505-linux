# SPDX-License-Identifier: MIT
# GoClever Navio 505 Linux: build entry points. See README.md.
PYTHON ?= python3
JOBS   ?= $(shell nproc 2>/dev/null || echo 4)

.PHONY: all fetch busybox initramfs kernel image test test-offline test-init clean distclean

all: image

fetch:
	$(PYTHON) scripts/fetch.py $(if $(FROM),--from $(FROM))

busybox:
	$(PYTHON) scripts/build-busybox.py

initramfs: busybox
	$(PYTHON) scripts/build-initramfs.py

kernel: initramfs
	$(PYTHON) scripts/build-kernel.py --jobs $(JOBS)

image: kernel
	$(PYTHON) scripts/package.py

test: test-init test-offline

test-offline:
	$(PYTHON) tests/test-offline.py $(if $(EBOOT),--eboot $(EBOOT))

test-init:
	$(PYTHON) tests/test-init.py

clean:
	rm -rf build/kernel build/busybox build/initramfs build/handoff out

distclean:
	rm -rf build toolchain downloads out
