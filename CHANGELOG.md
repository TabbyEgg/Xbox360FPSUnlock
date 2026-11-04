# 0.3.0 (development prototype)

- Add built-in ISO extraction and direct preparation for supported XISO/XGD layouts.
- Add small TU inspection/extraction without needing a game dump or XexTool.
- Accept patch/library modules without execution metadata, while retaining strict
  identity checks for the main game executable and supplied TU package.
- Add a Neighborhood-compatible XBDM panel for browsing, pulling, uploading a new
  copy with SHA-256 readback, and launching it as a separate action.
- Add ISO/TU regression coverage and loopback XBDM protocol tests.
- Add repository ignore rules, contribution notes and automated test configuration.

A real RSV2 TU was inspected read-only. Custom TU rebuilding, Windows EXE
packaging, real console transport and actual game runtime remain unvalidated.
No custom TU builder is included in this version.

# 0.2.0

- Focus the included workflow on Tom Clancy's Rainbow Six Vegas 2.
- Read GOD headers and their `.data` fragments directly, with hash checks.
- Prepare GOD executable modules and export a complete extracted game folder.
- Add an offline game catalog with separate included/reference status.
- Scan local extracted/GOD installations and select them from a game list.
- Retain TU merging before patching and preserve case when replacing assets.
- Expand synthetic automated coverage from 30 to 42 passing tests.

Windows GUI, actual XexTool processing, real GOD/TU inputs and Xbox 360 runtime
still need validation. RSV2's two supplied signatures must match the chosen build;
there is no automatic port when a TU changes those bytes.
