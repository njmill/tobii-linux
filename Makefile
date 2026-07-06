.PHONY: all runtime services dashboard headless status clear-logs preflight \
	sc-tobii-stock-runtime sc-tobii-stock-runtime-services \
	sc-tobii-stock-runtime-dashboard sc-tobii-stock-runtime-headless \
	sc-tobii-stock-runtime-status sc-tobii-stock-runtime-clear-logs \
	sc-tobii-stock-preflight sc-tobii-stock-install-launch-hook \
	sc-tobii-stock-disable-launch-hook \
	mediapipe-python mediapipe-venv mediapipe-fetch-model \
	install-launch-hook disable-launch-hook install-udev-rule clean

CC ?= cc
MINGW64_CC ?= x86_64-w64-mingw32-gcc
CFLAGS ?= -O2 -Wall -Wextra
SC_TOBII_STOCK_RECON_DIR ?= .tmp/sc-tobii-stock-recon

LIBUSB_CFLAGS := $(shell pkg-config --cflags libusb-1.0 2>/dev/null)
LIBUSB_LIBS := $(shell pkg-config --libs libusb-1.0 2>/dev/null)
OPENSSL_CFLAGS := $(shell pkg-config --cflags openssl 2>/dev/null)
OPENSSL_LIBS := $(shell pkg-config --libs openssl 2>/dev/null)

all: build/tobii-ttp-mux $(SC_TOBII_STOCK_RECON_DIR)/tobii-middleware-pipe-spy.exe

build/tobii-ttp-mux: tools/probes/tobii-ttp-mux.c
	mkdir -p build
	$(CC) $(CFLAGS) $(LIBUSB_CFLAGS) $(OPENSSL_CFLAGS) -o $@ $< $(LIBUSB_LIBS) $(OPENSSL_LIBS)

$(SC_TOBII_STOCK_RECON_DIR)/tobii-middleware-pipe-spy.exe: tools/app/tobii-middleware-pipe-spy.c
	mkdir -p "$(SC_TOBII_STOCK_RECON_DIR)"
	$(MINGW64_CC) -O2 -Wall -Wextra -o "$@" "$<" -lws2_32

mediapipe-python:
	./scripts/recon/setup-mediapipe-python.sh

mediapipe-venv:
	./scripts/recon/setup-mediapipe-venv.sh

mediapipe-fetch-model:
	./scripts/recon/fetch-mediapipe-face-landmarker.sh

preflight:
	./scripts/app/check-sc-stock-tobii-dll.sh

runtime: all mediapipe-venv mediapipe-fetch-model preflight
	./scripts/app/run-sc-tobii-native-runtime.sh dashboard

sc-tobii-stock-runtime: runtime

headless: all mediapipe-venv mediapipe-fetch-model preflight
	./scripts/app/run-sc-tobii-native-runtime.sh headless

sc-tobii-stock-runtime-headless: headless

services: $(SC_TOBII_STOCK_RECON_DIR)/tobii-middleware-pipe-spy.exe preflight
	./scripts/app/run-sc-tobii-native-runtime.sh services

sc-tobii-stock-runtime-services: services

dashboard: build/tobii-ttp-mux mediapipe-venv mediapipe-fetch-model
	SC_TOBII_STOCK_DLL_PREFLIGHT=0 ./scripts/app/run-sc-tobii-native-runtime.sh dashboard

sc-tobii-stock-runtime-dashboard: dashboard

status:
	./scripts/app/analyze-sc-tobii-runtime-logs.py --dir "$${SC_TOBII_NATIVE_RUNTIME_DIR:-.tmp/sc-tobii-native-runtime}"

sc-tobii-stock-runtime-status: status

clear-logs:
	./scripts/app/clear-sc-tobii-runtime-logs.sh

sc-tobii-stock-runtime-clear-logs: clear-logs

sc-tobii-stock-preflight: preflight

install-launch-hook:
	./scripts/app/install-sc-tobii-native-launch-hook.sh

sc-tobii-stock-install-launch-hook: install-launch-hook

disable-launch-hook:
	./scripts/app/disable-sc-tobii-native-launch-hook.sh

sc-tobii-stock-disable-launch-hook: disable-launch-hook

install-udev-rule:
	sudo install -m 0644 configs/udev/99-tobii-eyetracker5.rules /etc/udev/rules.d/99-tobii-eyetracker5.rules
	sudo udevadm control --reload-rules
	sudo udevadm trigger
	@echo "udev rule installed. Replug the Tobii device or reboot if permissions do not update."

clean:
	rm -rf build
