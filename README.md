# FreeCAD QET Routing

QET Routing is an external FreeCAD workbench that turns QElectroTech
connectivity into editable physical terminal locations, corridor-constrained
3D wire centerlines, and a wire-length schedule. It does not require a fork or
patch of FreeCAD.

## Current MVP

- accepts QET project formats 0.7 through 0.100 and imports current UUID and
  legacy numeric conductor endpoints;
- matches QET devices to physical FreeCAD objects by exact `Label`;
- stores Manufacturer, Article number, Order number, and Internal number from
  their distinct QET fields;
- creates reusable part types and shared terminal definitions;
- initializes terminals at the center of the matched part;
- propagates an inherited terminal move to every instance of the same part
  type, with an instance override when required;
- clamps moved terminals to the owning part's local bounding box;
- creates translucent, axis-aligned routing corridors;
- filters corridors by conductor section and bundle fill capacity;
- routes multiline conductors with A* and creates persistent
  `Part::FeaturePython` wire centerlines;
- reports geometric length and reactive cut length, including slack and end
  allowances, in a FreeCAD Spreadsheet;
- preserves valid routes on an unchanged reimport and marks removed QET
  records as obsolete instead of silently routing stale wires.

Single-line conductors, unknown conductor types, ambiguous identifiers,
unmatched parts, and recognized nonphysical QET report/slave symbols are
imported or diagnosed but are not routed.

## Installation

### Addon Manager custom repository

Add this repository URL and branch as a custom workbench repository in
FreeCAD's Addon Manager:

```text
Repository: https://github.com/maciej-kulon/FreeCAD-QET-Routing.git
Branch: main
```

Restart FreeCAD and select **QET Routing** from the workbench selector.

### Development checkout

Clone the repository into FreeCAD's user `Mod` directory using the install
folder name `QetRouting`, or add the repository root to FreeCAD's Python module
search path. FreeCAD 1.0 discovers the namespaced
`freecad.QetRouting.init_gui` entry point.

## Workflow

1. Give each physical FreeCAD part the exact `Label` used by its QET element.
2. Run **QET Routing → Import QElectroTech project**.
3. Ctrl-select one orange terminal plus a target vertex, edge, face, or object,
   then run **Place terminal on selected geometry**. Editing the terminal's
   `Placement` directly also works. Its shared `LocalPosition` updates every
   matching part instance; set `PositionMode` to `Overridden` for an exception.
4. Create corridors and edit their `Length`, `Width`, `Height`, `Placement`,
   `AllowedSections`, `Capacity`, and `MaxFillPercent`.
5. Set a conductor's `OuterDiameter` when bundle occupancy should use insulated
   diameter instead of copper cross-section.
6. Run **Route multiline conductors**.
7. Adjust route `SlackPercent` and `EndAllowanceEach` if needed, then run
   **Create wire-length schedule**.

QET's 2D terminal coordinates are used only to normalize legacy schematic
identity. They are never treated as physical 3D pin locations.

## Deliberate MVP limits

- Corridors must be axis-aligned.
- Routes are centerlines; bend radii, wire solids, clips, ducts, and obstacle
  avoidance are not implemented yet. Wires may share the same centerline; no
  clearance or lane-allocation rules are enforced.
- Terminal lead-ins connect each part to its nearest eligible corridor.
- Every QET multiline conductor endpoint pair becomes one physical wire.
- QET single-line conductors require a future explicit physical-core expansion
  workflow.
- Cross-folio report continuation and master/slave physical-device grouping are
  blocked pending an explicit mapping workflow.
- Connected pins from external QET element libraries can be recovered from
  current conductor UUIDs, but their `link_type` cannot be verified without the
  definition. Review the import warning before routing; disconnected pins
  require an embedded definition or a future library resolver.
- QET terminal-strip bridges and reserved terminals, `links_uuids`, cable
  grouping, and bus grouping are not interpreted.
- Corridor fill is a planning estimate, not standards-compliant duct sizing.
  Set each wire's `OuterDiameter`; otherwise copper section is used, and a
  corridor `Capacity` of zero means unlimited.
- QET device labels must be globally unique for automatic matching. Plant and
  location are retained as metadata but are not yet used to disambiguate.
- One synchronized QET project is supported per FreeCAD document.

## Verification

The dependency-free domain and routing tests run with:

```shell
python3 -m unittest discover -s tests -v
flake8 freecad tests --max-line-length=100
pyflakes freecad tests
```

The end-to-end test is executed with FreeCAD's bundled `freecadcmd`:

```shell
freecadcmd -P /path/to/FreeCAD-QET-Routing \
  -c "import runpy; runpy.run_path('tests/freecad_smoke.py', run_name='__main__')"
```

It covers import, shared and overridden terminals, bounds clamping, corridor
routing, failed-route cleanup, reactive cut length, schedule generation,
reimport synchronization, save, and reopen.

## Code layout

- `freecad/QetRouting/qet/`: immutable QET model, safe parser, diagnostics
- `freecad/QetRouting/document.py`: FreeCAD persistence and reusable terminals
- `freecad/QetRouting/routing.py`: FreeCAD-independent corridor graph and A*
- `freecad/QetRouting/routing_document.py`: FreeCAD route geometry and reports
- `freecad/QetRouting/commands.py`: workbench commands

## License

LGPL-2.1-or-later.
