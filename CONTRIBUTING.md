# Contributing

Run from the repository root with Python 3.10 or newer:

```sh
python -m unittest discover -v
python -m compileall -q rghfps tests run_workbench.pyw
```

The tests generate synthetic inputs and use a loopback TCP server. They need no
game downloads, console connection, XexTool, SDK or third-party Python packages.
The GUI additionally requires Tcl/Tk.

Keep game executables, title-update packages, disc images, GOD fragments,
credentials and external tool binaries out of commits. Reproduce format bugs
with a small synthetic fixture where possible.

For a game recipe, include its Title ID, source credit, exact long signatures
and expected/replacement bytes. Explain the matching builds and test status.
Treat reference catalog entries separately from ready recipes. Never substitute
an old fixed offset when a new build fails signature matching.

Describe console observations separately from parser/unit-test results. A byte
match does not establish measured FPS, mission stability or TU resource loading.
Modified TU generation is still an open implementation task.
