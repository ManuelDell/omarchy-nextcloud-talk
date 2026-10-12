.PHONY: validate test manifest lint demo

OMARCHY_SHELL_DIR ?= /usr/share/omarchy/shell
QMLLINT ?= $(shell command -v /usr/lib/qt6/bin/qmllint 2>/dev/null || command -v qmllint)
# Arch installs the Qt tools outside PATH.
export PATH := $(PATH):/usr/lib/qt6/bin

validate: test manifest lint

test:
	python3 -m py_compile bin/omanctalk bin/omanctalk_core.py bin/nctalk/*.py
	python3 -B -m unittest discover -s tests -v
	node plugins/omanctalk/tests/models.cjs
	python3 -m json.tool manifest.json >/dev/null

manifest:
	omarchy plugin validate .

# Validate every shipped component, including future additions.
lint:
	$(QMLLINT) -I "$(OMARCHY_SHELL_DIR)" $(wildcard plugins/omanctalk/*.qml plugins/omanctalk/*.js)

# Open the full app with repository-owned demo data (safe for screenshots).
demo:
	omarchy-shell io.github.manueldell.omanctalk openApp '{"demo":true}'
