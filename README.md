# Xbox360FPSUnlock

RGH FPS Workbench 0.3.0 — development prototype

A local desktop workbench for the awkward part of Xbox 360 FPS modding:
copying an executable, applying the right title update, decrypting/decompressing,
finding documented byte signatures, checking every edit, and saving a separate
candidate. It replaces manual HxD offset hunting with reusable recipes.

The current target is **Tom Clancy's Rainbow Six Vegas 2**, Title ID `55530807`.
Its supplied two-signature recipe is included and automatically selected when
you prepare that game.

This package contains Python source and a Windows launcher. **It is not a
precompiled Windows EXE.** There are no game executables, keys or XexTool binaries
inside it. The optional Neighborhood/XBDM panel connects directly to a console
on your local network. Other operations work locally.

## Start on Windows

1. Clone this repository or download and extract its source ZIP to your PC.
2. Install standard **Python 3.10 or newer**, including the Python launcher and
   Tcl/Tk. No third-party Python packages are needed to run the app.
3. Supply your own compatible **XexTool**. Put `xextool.exe` in `tools`, or use
   the app's Browse button. Its documentation and releases are linked below.
4. Double-click **Start-Workbench.cmd**.

Use a local NTFS folder for projects and exports. The tool uses no-overwrite,
atomic file publication, which needs hard-link support. Do not run it directly
inside a ZIP or create projects inside the original game's folder.

`Build-Windows-EXE.cmd` is optional. It creates a local Python environment,
downloads PyInstaller, and builds a one-file Windows app in `dist`. This build
script has not been run on Windows here. XexTool remains a separate dependency.

## ISO extraction

Use **ISO…** to choose a bare XISO or a supported full-disc XGD1/XGD2/XGD3
image. **Extract ISO / GOD only** creates a complete extracted game without
XexTool. Alternatively, prepare directly from an ISO: preparation reads modules
only, and export streams the complete game into a new folder. Keep the source
image available until export finishes.

The reader checks directory bounds and filenames; it does not authenticate an
ISO. Split/compressed images are not supported. Output is an extracted folder,
not a repacked ISO.

## GOD installations

Use **GOD…** to select the small, usually extensionless package header next to
its matching `<header-name>.data` folder. Keep every `Data0000`, `Data0001`, etc.
fragment with it. The header alone does not contain the game.

GOD extraction is built in: no GOD2ISO or intermediate ISO is required. Standard
split SVOD, enhanced GDF and supported self-contained layouts are read without
modifying source files. Header, master-chain, subtable and accessed data hashes
are checked. Container signatures are not verified.

Preparation extracts executable modules, then merges the selected TU and uses
XexTool to normalize those modules. **Export** streams the complete game into a
new `game/` folder and applies the patch there. Launch that extracted copy
through Aurora's file manager or add it to Aurora's scanned game paths.

Output is a complete **extracted game folder**, not a rebuilt GOD package. The
original GOD installation stays intact; keep it available until export finishes.
Allow space for the whole extracted game. Missing fragments and unknown layouts
stop the operation.

## Game lists

The **Games** tab separates included recipes from community references:

- **Rainbow Six Vegas 2** has an included recipe, ready for exact-signature
  scanning. This does not imply console testing or support for every TU.
- **Show community references** enables an offline list of 501 build entries
  across 318 Title IDs from Xenia Canary's patch repository. These can include
  non-FPS fixes and are not ready console patch recipes.

**Scan my game folder** finds extracted games, GOD installations and supported
ISO images. It displays their format/Media ID and lets you select an installation. The offline
catalog is also in `GAME-LIST.csv`. Other games need their own matching recipes.
To add a permanent ready recipe in the source version, place a valid signature
or bound JSON recipe in `recipes/` and restart the app. Import JSON remains
available in the patch tab.

## Normal workflow

### 1. Prepare

Choose the **extracted game folder** containing `default.xex`, the XEX alone,
a **GOD header**, or an **ISO**. Select the matching TU and your XexTool
installation. Choose a new project folder and click **Prepare copy**.

The title-update input can be:

- An STFS `LIVE`, `PIRS` or `CON` title-update package, including extensionless TU
  files. The built-in reader validates the relevant hash tables, data hashes,
  block chains, metadata and extraction paths. It does not verify signatures.
- An extracted TU folder with `default.xexp` in its root.
- A loose `default.xexp`. This supplies the executable patch only; the app
  explicitly records that no other TU assets were supplied.

Preparation copies the originals, checks game/update identifiers and delta
source versions, applies executable updates with XexTool's `-p` and `-u`, and
then decrypts/decompresses the results with `-c u -e u`. It checks the resulting
identity and target version. Media IDs are preserved, not zeroed to force a match.

If the TU patches additional `.xex` or `.dll` modules, select the complete game
folder, or use GOD/ISO input. Those originals are copied and updated too. Full
replacement executable updates and automatic TU downloads are outside this release.

### 2. Scan and preview

Select a recipe and click **Scan recipe**. The app scans the TU-updated,
normalized copy. Each long signature must appear exactly once. Missing or
ambiguous signatures stop the operation. Offsets from an older build are never
used as a fallback.

All locations are found before any bytes change. This matters for the supplied
Rainbow Six Vegas 2 edits: the first replacement produces bytes also used as
the second edit's original value. A simple repeated search/replace would be
unsafe; separate long signatures identify the two locations.

The preview shows every file offset, original byte sequence and replacement.
The generated recipe is bound to the prepared file's Title ID, Media ID,
version and SHA-256, with expected bytes for each edit.

### 3. Export

**Export patched copy** creates a new folder containing:

- `game/`: complete extracted and patched game when the input was GOD or ISO.
- `game_overlay/`: candidate modules and TU assets when the input was an
  extracted game or XEX; apply this overlay to a separate complete game copy.
- `applied-recipe.json`: exact edits and source credit.
- `receipt.json`: build identifiers, hashes and validation result.
- `READ-BEFORE-TESTING.txt`: how this candidate relates to the original and TU.

Your preparation project includes clean original module copies for restoration.
The optional console panel can upload the candidate under a new name and launch
it as a separate action. It does not change Aurora settings.

## Paste a creator's instructions

Click **Paste instructions**, paste the description, supply a recipe name and
source credit, and click **Find edits in prepared copy**. Supported descriptions
contain explicit lines such as:

    change 81 5F 35 0C to 39 40 00 01

Each edit also needs a long exact hex signature containing the original bytes.
Wrapped descriptions are supported, including the supplied RSV2 description.
The parser treats pasted text as data, never as executable commands. It does not
infer a limiter from a numeric FPS value or replace every matching short opcode.

Save the resulting bound recipe to reuse it on the **same exact build**. A JSON
signature profile may also be imported to locate the documented pattern on
another prepared build. Matching a new TU's bytes is not runtime validation.

## Compare an existing patched XEX

If a creator supplied a patched executable, **Compare patched XEX** normalizes
that reference through XexTool and compares it with the prepared original.
Title ID, Media ID, version, payload layout and length must match. The tool
captures small payload differences and excludes header/signature changes.
It never treats this as automatic proof that the changes only affect FPS.

A TU0 original/patched pair cannot automatically port a patch to TU1. Compare
mode rejects that version mismatch. A new matching signature or actual analysis
of the updated code is required.

## Included game recipes

| Game | Included support |
| --- | --- |
| Rainbow Six Vegas 2, Title ID `55530807` | Two long signatures and replacements transcribed from Xbox360forever's instructions. No game/TU hardware test has been performed. |

Other games can be inspected/prepared, but need their own recipes. The two
RSV2 edits are not universal FPS patches.

## Inspect a TU without extracting a game

**Inspect TU only** lists the package identity, contents and executable/delta
versions. **Extract TU only** extracts that small package without XexTool or a
game dump. Encrypted delta patches may lack their own execution-information
header; their absent identifiers stay unknown rather than being invented.

**Modified TU rebuilding is not implemented.** A title update is an STFS
container, not a ZIP. The inspected RSV2 update contains `default.xexp`,
`LinkApp.xexp`, `dmdcp_xbox360.dllp` and `dmsdk_xbox360.dllp`: all are delta
patches, with no standalone `default.xex`. Its code cannot be treated as a full
executable and patched by simply replacing properties or rezipping it.

The current FPS export path uses an extracted game copy. It cannot yet apply the
FPS changes to an existing GOD installation by sending only a compact modified TU.

## Neighborhood / XBDM

The **Neighborhood / XBDM** tab uses the console's XBDM service directly. Enter
the IP address from your working Neighborhood setup, normally on port `730`.
The console must have XBDM running and be reachable from the PC. This app needs
no additional Python library or Microsoft SDK installation; it does not install
or supply the console plugin.

- Browse storage drives and pull a game folder, XEX or TU to the PC. Pulling a
  GOD header also fetches its matching `.data` folder when required.
- Upload a patched XEX under a new filename, or a complete extracted game under
  a new folder name. Uploads use staging names and SHA-256 readback checks.
- **Launch verified copy** is a separate action. The executable is checked again
  before the launch request. An overlay folder alone is not a complete game.

Existing destination files/folders are rejected. A failed folder upload can
leave its new staging folder behind; its path is reported. Console transport
has loopback protocol tests but has not been tested against an actual Xbox 360.

## What title-update support means here

The candidate executable contains the selected TU code **before** the FPS edit.
Additional supplied resources are retained, including auxiliary module updates.
The original update package is preserved in the preparation project when supplied.

This release does not prove that a cached TU will be skipped by every console
launch path. Do not manually apply the same `.xexp` again to the embedded-update
candidate. Test in a separate game copy and verify the intended update-loading
setup. Games that use the console's `update:` mount for assets may need a
game-specific arrangement; retaining files in an overlay alone cannot prove
equivalent resource loading.

An exact signature match proves a byte location, not correct FPS, mission
behavior, game speed or compatibility with every TU. Outputs remain marked
**candidates for console testing**. The app does not measure performance.

## Optional command line

From this package's folder:

```powershell
py -3 -m rghfps inspect "D:\Games\RSV2\default.xex"
py -3 -m rghfps prepare --game "D:\Games\RSV2" --tu "D:\Updates\RSV2-TU" --tool "D:\Tools\xextool.exe" --output "D:\Workbench\RSV2-project"
py -3 -m rghfps scan --project "D:\Workbench\RSV2-project" --recipe "recipes\rainbow-six-vegas-2.json" --save-recipe "D:\Workbench\RSV2-bound.json"
py -3 -m rghfps export --project "D:\Workbench\RSV2-project" --recipe "D:\Workbench\RSV2-bound.json" --output "D:\Workbench\RSV2-export"
py -3 -m rghfps games --query "rainbow"
py -3 -m rghfps games --references --query "gears"
py -3 -m rghfps games --folder "D:\Games"
py -3 -m rghfps extract --image "D:\Images\RSV2.iso" --output "D:\Workbench\RSV2-extracted"
py -3 -m rghfps tu-info "D:\Updates\RSV2-TU"
py -3 -m rghfps tu-extract "D:\Updates\RSV2-TU" --output "D:\Workbench\RSV2-TU-contents"
```

Output paths must be new. Errors stop the pipeline, retain a preparation failure
report when applicable, and leave original inputs alone.

## Validation and limits

Run `py -3 -m unittest discover -v` (or `python3` on Linux). Tests use synthetic
XEX/STFS/GOD/ISO fixtures, including multi-level and secondary hash tables. They cover
both supplied edits, pasted descriptions, shifted/ambiguous signatures,
hash/version/byte mismatches, original preservation, TU ordering, auxiliary
modules, receipts, and a real subprocess wrapper around a synthetic XexTool stub.
GOD tests cover split/enhanced/embedded layouts, fragment boundaries, nested and
multi-sector directories, full-game exports with TU assets, corruption and local
inventory scans. ISO tests cover all four supported partition offsets. XBDM
tests use an in-process TCP server for binary framing, transfers, verification,
new destinations and explicit launch.

A supplied real RSV2 TU was also inspected and extracted read-only, with STFS
hash checks. Its four encrypted delta modules were recognized. That file is not
included in this repository, and this is not a real XexTool merge test.

**Actual XexTool encryption/delta processing, Windows GUI launch and Xbox 360
runtime behavior have not been tested in this environment.** Real ISO/GOD inputs
and real console connections have not been tested. See `VALIDATION.txt` for results.

Limits: 512 MiB per executable/extracted update, 16 GiB extracted disc contents,
128 GOD fragments, 100,000 GOD directory entries, 8192 TU files, 32 signature edits,
512 bound edits and 8192 bytes changed per patch. FPS edits require complete,
normalized XEX2 executables. Delta patches can be inspected and merged with
matching original modules through XexTool. Header modifications and in-place
patching are disabled.

## Sources and credit

- Xbox360forever: https://www.youtube.com/@xbox360forever3
- Tutorial: https://www.youtube.com/watch?v=W26KD-mntEY
- The included RSV2 signatures were transcribed from that creator's instructions.
- XexTool command documentation: https://github.com/Team-Resurgent/XexTool
- Xenia XEX2 structures: https://github.com/xenia-project/xenia/blob/master/src/xenia/kernel/util/xex2_info.h
- Xenia STFS structures/address calculations: https://github.com/xenia-project/xenia/blob/master/src/xenia/vfs/devices/stfs_xbox.h
  and https://github.com/xenia-project/xenia/blob/master/src/xenia/vfs/devices/stfs_container_device.cc
- RSV2 Title ID: https://github.com/xenia-project/game-compatibility/issues/1024
- GOD format/hash cross-check: https://github.com/iliazeus/iso2god-rs/tree/master/src/god
- XBDM protocol references: https://github.com/Experiment5X/XBDM and https://github.com/MobCat/xbdm.py
- Community reference catalog: https://github.com/xenia-canary/game-patches
  at commit `fe957a050e37f5a1e3f485ca43c2e37ee2a9c92d`.

Source code is MIT-licensed except where noted. STFS format calculations follow
Xenia; its BSD notice is preserved in `THIRD_PARTY_NOTICES.txt`. XexTool is a
separate project under its own license and is not bundled or endorsed here.
