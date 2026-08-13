.PHONY: all runtime runtime-check test

all: runtime

# A relocatable launcher closure used by kilix-content. The launchers retain
# argv boundaries and point back into the selected immutable checkout.
runtime:
	KILIX_TUI_UTILS_PREFIX="$(CURDIR)/.runtime" \
	KILIX_TUI_UTILS_RELOCATABLE=1 \
	KILIX_TUI_UTILS_SYNC_MENU=0 ./install.sh

runtime-check: runtime
	test -x .runtime/bin/kilix-file
	test -x .runtime/bin/kilix-system-center
	test -x .runtime/bin/kilix-settings-center
	test -x .runtime/bin/kilix-software-center
	test -x .runtime/bin/kilix-session-center
	test -x .runtime/bin/kilix-voice-studio
	test -x .runtime/bin/kilix-character-map
	test -x .runtime/bin/kilix-notepad
	test -x .runtime/bin/kilix-find-files

test:
	python3 tests/run.py
