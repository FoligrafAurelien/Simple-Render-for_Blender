# SimpleRender — Blender 5.0.1 Plugin

> Batch-render every collection of your scene, locally or on a render farm. No farm agent required.

**Author:** Aurelien Binauld aka Foligraf  
**Version:** 2.2.0  
**Blender:** 5.0.1+  
**Category:** Render

---

## What it does

SimpleRender splits a complex Blender scene into isolated render jobs — one per asset collection — and lets you either launch them locally in a visible terminal or export them as standalone `.blend` files for any render farm (Deadline, Tractor, Royal Render, etc.).

The key concept is a two-tier collection system:

| Type | Role |
|---|---|
| **Always Rendering** | Included in **every** job — camera, lights, background, ground plane |
| **One Time Render** | Gets its **own** dedicated job — one asset or group per render |

---

## Installation

1. Download `simple_render.py`
2. Open Blender → **Edit › Preferences › Add-ons › Install…**
3. Select `simple_render.py` and confirm
4. Enable **SimpleRender** in the add-on list
5. Open the **N-Panel** (`N` key in the 3D Viewport) → tab **Simple Render**

---

## UI Overview

```
┌─ Simple Render ───────────────────────────────┐
│                                               │
│  📷 Active Camera     Camera.001              │
│                                               │
│  📁 Output                                    │
│  Output Folder  [ //renders/             ]    │
│  Prefix         [ render                 ]    │
│    →  render_MyAsset_0001.ext                 │
│    Jobs: Output Folder/_jobs/                 │
│                                               │
│  🕐 Frame Settings                            │
│  [ Single Frame ]  [ Frame Range ]            │
│  Frame Number  1                              │
│                                               │
│  🎬 Image Sequence                            │
│  ☐ Overwrite    ☐ Placeholders               │
│                                               │
│  📦 Collections                          ↻    │
│  Name              Always    One Time         │
│  ├─ Camera_Rig       ☑          ☐            │
│  ├─ Lights           ☑          ☐            │
│  ├─ Background       ☑          ☐            │
│  ├─ Asset_A          ☐          ☑            │
│  ├─ Asset_B          ☐          ☑            │
│  └─ Asset_C          ☐          ☑            │
│  Always: 3   One Time: 3  →  3 job(s)        │
│                                               │
│  [▶  Launch Local Renders            ]        │
│  [    Generate Separate Blender Files ]       │
└───────────────────────────────────────────────┘
```

---

## Options Reference

### Output

| Field | Description |
|---|---|
| **Output Folder** | Root folder for rendered images. Supports Blender's `//` relative prefix. Generated `.blend` job files are saved in `{Output}/_jobs/` automatically. |
| **Prefix** | Image filename prefix. Final name: `{prefix}_{collection}_{frame:04d}.ext` |

### Frame Settings

| Option | Description |
|---|---|
| **Single Frame** | Render one specific frame number |
| **Frame Range** | Render a range of frames with Start and End fields |
| **Get Blender's Range** | One-click copy of the scene's `frame_start` / `frame_end` into SimpleRender |

### Image Sequence

These two options mirror Blender's native **Output › Image Sequence** settings and are baked into every generated job:

| Option | Default | Description |
|---|---|---|
| **Overwrite** | ☐ Off | Re-render and overwrite frames that already exist on disk. Disabled by default to protect renders in case of crash. |
| **Placeholders** | ☐ Off | Create empty placeholder files during rendering. Prevents two farm workers from rendering the same frame simultaneously. |

### Collections

| Column | Default | Description |
|---|---|---|
| **Always** | ☐ Off | This collection is included in **every** render job. Use for shared assets: camera rig, lighting setup, background, HDRI sphere, etc. At least one must be checked to guarantee the camera is present. |
| **One Time** | ☑ On | A dedicated job is generated for this collection. Each One Time collection is rendered alongside all Always collections. |

Use the **↻** button to rescan the scene at any time. Previously assigned checkboxes are preserved on refresh — only newly discovered collections receive the default values.

---

## Action Buttons

### ▶ Launch Local Renders

Generates a `launch_render.bat` (Windows) or `launch_render.sh` (Linux/macOS) and launches it immediately in a **visible terminal window**. The artist can watch the Blender render log in real time. Collections are rendered sequentially, one after the other.

**How it works:**
- Saves the `.blend` file
- Writes one isolated Python worker script per collection (`_sr_job_{name}.py`) in the output folder — no shell escaping issues regardless of collection name
- Builds the `.bat` / `.sh` with one `blender --background --python` call per collection
- Launches the script in a new CMD / Terminal window that stays open at the end (`pause` / `read`)

**No separate `.blend` files are created** — Blender reads the original scene and the worker script toggles collection visibility on the fly.

### Generate Separate Blender Files

Generates one standalone `.blend` file per One Time Render collection, saved in `{Output}/_jobs/`. Each file is fully self-contained:

- Only the target collection + Always collections are present in the scene (unused collections are **physically removed** from the hierarchy, not just hidden)
- Render output path, frame range, Overwrite and Placeholders settings are baked in
- No plugin required on the render farm — Deadline or any farm manager can submit these as standard Blender jobs

A confirmation popup displays the number of files generated and their location. The `_jobs/` folder opens automatically in your OS file explorer.

---

## Workflow Examples

### Local batch render (artist workstation)

1. Set up collections: tick **Always** on your Camera + Lights collection, **One Time** on each asset
2. Set output folder and prefix
3. Click **▶ Launch Local Renders**
4. A terminal opens and renders each asset sequentially

### Deadline / farm submission

1. Same collection setup as above
2. Click **Generate Separate Blender Files**
3. Open `_jobs/` — one `.blend` per asset, ready to drag into Deadline
4. Submit as standard Blender jobs, no plugin needed on the farm

---

## File Output Structure

```
📁 renders/                          ← Output Folder
 ├── render_Asset_A_0001.png         ← Rendered frames
 ├── render_Asset_A_0002.png
 ├── render_Asset_B_0001.png
 ├── launch_render.bat               ← Generated by Launch Local Renders
 ├── _sr_job_Asset_A.py              ← Worker scripts (auto-generated)
 ├── _sr_job_Asset_B.py
 └── _jobs/                          ← Generated by Generate Separate Blender Files
      ├── Asset_A.blend
      ├── Asset_B.blend
      └── Asset_C.blend
```

---

## Technical Notes

### Collection unlink — not just hidden
When generating separate `.blend` files, excluded collections are **physically unlinked** from the scene hierarchy at every nesting level (recursive), not simply hidden via the view layer. This means the saved `.blend` contains no geometry, materials or textures from excluded collections — resulting in genuinely lighter files.

### Shell-safe collection names
Worker `.py` scripts pass collection names as Python string literals via `repr()`. The BAT/SH only references the script file path — never the collection name directly. Collections named `L'objet "Spécial" (v2)` or any other combination of special characters work without issue.

### Snapshot / restore pattern
Both buttons save and restore the entire scene state (collection visibility, render filepath, frame range, overwrite, placeholder) in a `try / finally` block. The original `.blend` is **never modified** by either operation.

---

## Limitations

- Only top-level visibility is toggled in `Launch Local Renders` mode. Deeply nested sub-collections within a visible Always or One Time collection inherit their parent's visibility.
- `Generate Separate Blender Files` requires the `.blend` to be saved on disk first (the plugin auto-saves before generating).
- Worker `.py` scripts (`_sr_job_*.py`) and the `.bat` / `.sh` are left in the output folder after rendering — they can be safely deleted manually.

---

## Changelog

### 2.2.0
- Collection names with special characters, quotes or accents now work correctly in all render modes (worker `.py` files instead of `--python-expr`)
- Deep recursive unlink: excluded collections at any nesting level are physically absent from generated `.blend` files
- Removed redundant "Generate Render Jobs" button (duplicate of Generate Separate Blender Files)
- Checkbox state preserved on collection list refresh
- Panel width increased (`bl_ui_units_x = 14`)
- `Overwrite` default changed to Off (safer for crash recovery)

### 2.1.0
- Always Rendering / One Time Render two-column collection system
- Aligned UIList headers with `split(factor)` matching `draw_item`
- `sanitize_name()` helper for safe filenames
- Recursive collection unlink in generated `.blend` files
- Success popup with output path after file generation
- OS file explorer opens automatically after generation

### 2.0.0
- Complete rewrite: generate one `.blend` per collection instead of BAT-only approach
- Farm-compatible output (no plugin required on render nodes)
- Snapshot / restore pattern for non-destructive operation

---

## License

MIT — free to use, modify and distribute. Credit appreciated.
