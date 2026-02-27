"""
SimpleRender - Blender 5.0.1 Plugin
Generates one render-ready .blend per "One Time Render" collection.
"Always Rendering" collections (camera, lights, BG…) are injected into every job.
No plugin required on the render farm — pure native Blender files.

Author  : Aurelien Binauld aka Foligraf
Version : 2.2.0
"""

bl_info = {
    "name": "SimpleRender",
    "author": "Aurelien Binauld aka Foligraf",
    "version": (2, 2, 0),
    "blender": (5, 0, 1),
    "location": "View3D > N-Panel > Simple Render",
    "description": "Generate farm-ready .blend files per collection for Deadline and other render managers",
    "category": "Render",
}

import bpy
import os
import re
import subprocess
import platform
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
#  UIList column split factors — must match between header and draw_item
# ─────────────────────────────────────────────────────────────────────────────
COL_NAME_FACTOR   = 0.60   # Name  takes 60 % of the row
COL_ALWAYS_FACTOR = 0.50   # of the remaining 40 %, Always takes half


# ─────────────────────────────────────────────────────────────────────────────
#  Per-collection list item
# ─────────────────────────────────────────────────────────────────────────────

class SR_CollectionItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()

    always_render: bpy.props.BoolProperty(
        name="Always",
        description=(
            "Always included in every render job — use for camera, lights, "
            "background, ground plane, etc.\n"
            "At least one must be checked to guarantee the camera is present"
        ),
        default=False,
    )
    one_time_render: bpy.props.BoolProperty(
        name="One Time",
        description="Generate a dedicated .blend render job for this collection",
        default=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Scene-level properties
# ─────────────────────────────────────────────────────────────────────────────

class SimpleRenderProperties(bpy.types.PropertyGroup):

    # ── Output ────────────────────────────────────────────────────────────────
    output_folder: bpy.props.StringProperty(
        name="Output Folder",
        description=(
            "Root output folder.\n"
            "  Rendered images  →  {output}/\n"
            "  Generated .blend jobs  →  {output}/_jobs/"
        ),
        subtype="DIR_PATH",
        default="//renders/",
    )
    file_prefix: bpy.props.StringProperty(
        name="Prefix",
        description="Image filename prefix  →  {prefix}_{collection}_{frame:04d}.ext",
        default="render",
    )

    # ── Frame mode ────────────────────────────────────────────────────────────
    render_mode: bpy.props.EnumProperty(
        name="Render Mode",
        items=[
            ("SINGLE", "Single Frame", "Render one specific frame",  "IMAGE_DATA",       0),
            ("RANGE",  "Frame Range",  "Render a range of frames",   "RENDER_ANIMATION", 1),
        ],
        default="SINGLE",
    )
    single_frame: bpy.props.IntProperty(
        name="Frame",
        description="Frame number to render",
        default=1, min=0,
    )
    range_start: bpy.props.IntProperty(name="Start", default=1,   min=0)
    range_end:   bpy.props.IntProperty(name="End",   default=250, min=0)

    # ── Image sequence options (mirrors Blender's Output > Image Sequence) ────
    use_overwrite: bpy.props.BoolProperty(
        name="Overwrite",
        description=(
            "Overwrite existing frame files when rendering.\n"
            "Maps to Blender's native Output > Overwrite setting in every generated job"
        ),
        default=False,
    )
    use_placeholder: bpy.props.BoolProperty(
        name="Placeholders",
        description=(
            "Create placeholder files while rendering.\n"
            "Prevents two farm workers from rendering the same frame simultaneously.\n"
            "Maps to Blender's native Output > Placeholders setting"
        ),
        default=False,
    )

    # ── UIList index ──────────────────────────────────────────────────────────
    collection_index: bpy.props.IntProperty(default=0)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def all_collections(scene) -> list:
    """Return ALL collections in the scene hierarchy, no name filtering."""
    result = []
    def _walk(col):
        for child in col.children:
            result.append(child)
            _walk(child)
    _walk(scene.collection)
    return result


def resolve_path(raw: str) -> Path:
    if raw.startswith("//") and bpy.data.filepath:
        return (Path(bpy.data.filepath).parent / raw[2:]).resolve()
    return Path(raw).resolve()


def find_layer_collection(layer_col, name: str):
    if layer_col.collection.name == name:
        return layer_col
    for child in layer_col.children:
        r = find_layer_collection(child, name)
        if r:
            return r
    return None


def sanitize_name(name: str) -> str:
    """
    Replace characters that are unsafe in filenames and shell paths with '_'.
    Used to build safe .py script filenames from collection names.
    The original collection name is passed safely inside the .py file itself
    as a Python string literal — no shell escaping needed.
    """
    return re.sub(r'[<>:"/\\|?*\'\s]', '_', name)


def deep_collect_to_unlink(parent_col, visible_cols: set) -> list:
    """
    Recursively walk the collection hierarchy and return a list of
    (parent_collection, child_collection) pairs where child is NOT in
    visible_cols.

    • Excluded collections are collected at the highest possible level so
      their entire subtree is removed in one unlink call.
    • Visible collections are walked recursively so nested excluded
      sub-collections inside a visible parent are also caught.

    This guarantees the saved .blend contains no geometry from excluded
    collections, regardless of nesting depth.
    """
    pairs = []
    for child in list(parent_col.children):
        if child.name not in visible_cols:
            # Collect at this level — entire subtree goes with it
            pairs.append((parent_col, child))
        else:
            # Visible parent → recurse to find hidden sub-collections
            pairs.extend(deep_collect_to_unlink(child, visible_cols))
    return pairs


def get_jobs_dir(sr) -> Path:
    return resolve_path(sr.output_folder) / "_jobs"



# ─────────────────────────────────────────────────────────────────────────────
#  Core generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_blend_for_collection(
    target_col_name:  str,
    always_col_names: list,
    blend_out_dir:    Path,
    image_out_dir:    Path,
    file_prefix:      str,
    start_frame:      int,
    end_frame:        int,
    sr,
) -> Path:
    """
    Snapshot current scene state → apply job-specific overrides → save copy → restore.

    The generated .blend has:
      • target collection  visible  (One Time Render)
      • always collections visible  (Always Rendering — camera, lights, etc.)
      • all other top-level collections excluded from view layer
      • render.filepath, frame_start, frame_end, use_overwrite, use_placeholder baked in
    """
    scene = bpy.context.scene
    vl    = bpy.context.view_layer

    # ── Snapshot ──────────────────────────────────────────────────────────────
    snap_exclusions   = {lc.collection.name: lc.exclude
                         for lc in vl.layer_collection.children}
    snap_filepath     = scene.render.filepath
    snap_frame_start  = scene.frame_start
    snap_frame_end    = scene.frame_end
    snap_overwrite    = scene.render.use_overwrite
    snap_placeholder  = scene.render.use_placeholder

    visible_cols = {target_col_name} | set(always_col_names)

    # Recursively collect ALL (parent, child) pairs to unlink — not just
    # root-level. This removes nested excluded sub-collections too, so the
    # saved .blend contains no geometry from collections outside visible_cols,
    # regardless of hierarchy depth.
    pairs_to_unlink = deep_collect_to_unlink(scene.collection, visible_cols)

    try:
        # ── Unlink excluded collections at every nesting level ────────────────
        for parent_col, child_col in pairs_to_unlink:
            parent_col.children.unlink(child_col)

        # ── Visibility (for the ones that remain) ─────────────────────────────
        for lc in vl.layer_collection.children:
            lc.exclude = (lc.collection.name not in visible_cols)
        for col_name in visible_cols:
            lc = find_layer_collection(vl.layer_collection, col_name)
            if lc:
                lc.exclude = False

        # ── Render output ─────────────────────────────────────────────────────
        image_out_dir.mkdir(parents=True, exist_ok=True)
        # Blender appends #### + extension; we provide the stem
        scene.render.filepath = str(image_out_dir / f"{file_prefix}_{target_col_name}_")

        # ── Frames ───────────────────────────────────────────────────────────
        scene.frame_start = start_frame
        scene.frame_end   = end_frame

        # ── Image sequence options ────────────────────────────────────────────
        scene.render.use_overwrite   = sr.use_overwrite
        scene.render.use_placeholder = sr.use_placeholder

        # ── Save copy (does NOT change bpy.data.filepath) ─────────────────────
        blend_out_dir.mkdir(parents=True, exist_ok=True)
        target_blend = blend_out_dir / f"{target_col_name}.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(target_blend), copy=True)

    finally:
        # ── Restore unlinked collections (reverse order, deepest first) ───────
        for parent_col, child_col in reversed(pairs_to_unlink):
            parent_col.children.link(child_col)

        # ── Restore view layer exclusions ─────────────────────────────────────
        for lc in vl.layer_collection.children:
            if lc.collection.name in snap_exclusions:
                lc.exclude = snap_exclusions[lc.collection.name]
        scene.render.filepath    = snap_filepath
        scene.frame_start        = snap_frame_start
        scene.frame_end          = snap_frame_end
        scene.render.use_overwrite   = snap_overwrite
        scene.render.use_placeholder = snap_placeholder

    return target_blend


# ─────────────────────────────────────────────────────────────────────────────
#  Operators
# ─────────────────────────────────────────────────────────────────────────────

class SR_OT_RefreshCollections(bpy.types.Operator):
    """Rescan the scene and rebuild the collection list (all collections, no filter)."""
    bl_idname  = "simplerender.refresh_collections"
    bl_label   = "Refresh Collections"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        items = context.scene.sr_collections

        # Snapshot current checkbox state before clearing
        prev_state = {item.name: (item.always_render, item.one_time_render)
                      for item in items}

        items.clear()
        for col in all_collections(context.scene):
            item              = items.add()
            item.name         = col.name
            # Restore previous state if collection was already in the list
            if col.name in prev_state:
                item.always_render,  item.one_time_render = prev_state[col.name]
            else:
                item.always_render   = False
                item.one_time_render = True
        self.report({"INFO"}, f"SimpleRender: {len(items)} collection(s) found.")
        return {"FINISHED"}


class SR_OT_GetBlenderRange(bpy.types.Operator):
    """Copy Blender scene frame range into SimpleRender fields."""
    bl_idname      = "simplerender.get_blender_range"
    bl_label       = "Get Blender's Range"
    bl_description = "Copy current scene Start / End frames"
    bl_options     = {"INTERNAL"}

    def execute(self, context):
        sr             = context.scene.simple_render
        sr.range_start = context.scene.frame_start
        sr.range_end   = context.scene.frame_end
        return {"FINISHED"}



class SR_OT_LaunchLocal(bpy.types.Operator):
    """
    Write a .bat/.sh that drives Blender in --background mode for each collection,
    then immediately open it in a visible terminal so the artist can watch progress.
    No separate .blend files are needed — collections are toggled via a Python snippet
    passed inline to each Blender process.
    """
    bl_idname  = "simplerender.launch_local"
    bl_label   = "Launch Local Renders"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene  = context.scene
        sr     = scene.simple_render
        items  = context.scene.sr_collections

        always_items   = [i for i in items if i.always_render]
        one_time_items = [i for i in items if i.one_time_render]

        if not one_time_items:
            self.report({"ERROR"}, "No 'One Time Render' collection selected.")
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the .blend file first.")
            return {"CANCELLED"}

        if sr.render_mode == "RANGE" and sr.range_start > sr.range_end:
            self.report({"ERROR"}, "Frame range: Start must be ≤ End.")
            return {"CANCELLED"}

        # Save source so --background picks up the latest state
        bpy.ops.wm.save_mainfile()

        blend_path   = Path(bpy.data.filepath)
        out_dir      = resolve_path(sr.output_folder)
        blender_bin  = bpy.app.binary_path
        is_windows   = platform.system() == "Windows"
        bat_ext      = ".bat" if is_windows else ".sh"
        bat_path     = out_dir / f"launch_render{bat_ext}"

        out_dir.mkdir(parents=True, exist_ok=True)

        start = end = sr.single_frame
        if sr.render_mode == "RANGE":
            start, end = sr.range_start, sr.range_end

        always_names   = [i.name for i in always_items]
        one_time_names = [i.name for i in one_time_items]

        overwrite   = sr.use_overwrite
        placeholder = sr.use_placeholder

        # ── Write one .py worker script per collection ────────────────────────
        # Using a .py file (instead of --python-expr) sidesteps ALL shell
        # escaping issues: the collection name lives as a Python string literal
        # inside the file — no quoting, no special-character problems.
        script_paths = {}
        for col_name in one_time_names:
            safe    = sanitize_name(col_name)
            output_path = str(out_dir / f"{sr.file_prefix}_{safe}_").replace("\\", "/")

            script_content = (
                "import bpy\n"
                f"col_name   = {repr(col_name)}\n"
                f"always     = {repr(always_names)}\n"
                f"visible    = always + [col_name]\n"
                "scene = bpy.context.scene\n"
                "vl    = bpy.context.view_layer\n"
                "for lc in vl.layer_collection.children:\n"
                "    lc.exclude = lc.collection.name not in visible\n"
                f"scene.render.filepath    = {repr(output_path)}\n"
                f"scene.frame_start        = {start}\n"
                f"scene.frame_end          = {end}\n"
                f"scene.render.use_overwrite   = {overwrite}\n"
                f"scene.render.use_placeholder = {placeholder}\n"
                "bpy.ops.render.render(animation=True)\n"
            )

            script_path = out_dir / f"_sr_job_{safe}.py"
            script_path.write_text(script_content, encoding="utf-8")
            script_paths[col_name] = script_path

        # ── Build BAT / SH ────────────────────────────────────────────────────
        lines = []
        if is_windows:
            lines += ["@echo off", "chcp 65001 > nul", ""]
            lines.append("echo ============================================")
            lines.append(f"echo  SimpleRender ^— {len(one_time_names)} collection(s)")
            lines.append("echo ============================================")
            lines.append("")
            for col_name in one_time_names:
                script_path = script_paths[col_name]
                lines.append(f"echo.")
                lines.append(f"echo --- Rendering: {sanitize_name(col_name)} ---")
                lines.append(
                    f'"{blender_bin}" --background "{blend_path}"'
                    f' --python "{script_path}"'
                )
                lines.append("")
            lines += [
                "echo.",
                "echo ============================================",
                "echo  All renders complete!",
                f"echo  Output folder: {out_dir}",
                "echo ============================================",
                "pause",
            ]
            content = "\r\n".join(lines)

        else:
            lines += [
                "#!/usr/bin/env bash",
                "echo '============================================'",
                f"echo ' SimpleRender — {len(one_time_names)} collection(s)'",
                "echo '============================================'",
                "",
            ]
            for col_name in one_time_names:
                script_path = script_paths[col_name]
                lines.append("echo ''")
                lines.append(f"echo '--- Rendering: {sanitize_name(col_name)} ---'")
                lines.append(
                    f'"{blender_bin}" --background "{blend_path}"'
                    f' --python "{script_path}"'
                )
                lines.append("")
            lines += [
                "echo ''",
                "echo '============================================'",
                "echo ' All renders complete!'",
                f"echo ' Output folder: {out_dir}'",
                "echo '============================================'",
                "read -p 'Press Enter to close...'",
            ]
            content = "\n".join(lines)

        bat_path.write_text(content, encoding="utf-8")
        if not is_windows:
            bat_path.chmod(bat_path.stat().st_mode | 0o111)

        # ── Launch in a visible terminal ──────────────────────────────────────
        try:
            if is_windows:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", str(bat_path)],
                    shell=False,
                )
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-a", "Terminal", str(bat_path)])
            else:
                for term in [
                    ["gnome-terminal", "--", "bash", str(bat_path)],
                    ["xterm", "-e", f"bash '{bat_path}'; read -p 'Press Enter...'"],
                ]:
                    try:
                        subprocess.Popen(term)
                        break
                    except FileNotFoundError:
                        continue
        except Exception as e:
            self.report({"WARNING"}, f"Scripts written but could not auto-launch: {e}")
            return {"FINISHED"}

        self.report({"INFO"},
            f"Render started — {len(one_time_names)} collection(s). Watch the terminal.")
        return {"FINISHED"}


class SR_OT_GenerateBlendFiles(bpy.types.Operator):
    """Generate one standalone .blend per One Time Render collection, saved in Output/_jobs/."""
    bl_idname  = "simplerender.generate_blend_files"
    bl_label   = "Generate Separate Blender Files"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        sr    = scene.simple_render
        items = context.scene.sr_collections

        always_items   = [i for i in items if i.always_render]
        one_time_items = [i for i in items if i.one_time_render]

        if not one_time_items:
            self.report({"ERROR"}, "No 'One Time Render' collection selected.")
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the .blend file first.")
            return {"CANCELLED"}

        if sr.render_mode == "RANGE" and sr.range_start > sr.range_end:
            self.report({"ERROR"}, "Frame range: Start must be ≤ End.")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()

        out_dir  = resolve_path(sr.output_folder)
        jobs_dir = out_dir / "_jobs"

        start = end = sr.single_frame
        if sr.render_mode == "RANGE":
            start, end = sr.range_start, sr.range_end

        always_names = [i.name for i in always_items]
        generated, errors = [], []

        for item in one_time_items:
            try:
                out = generate_blend_for_collection(
                    target_col_name  = item.name,
                    always_col_names = always_names,
                    blend_out_dir    = jobs_dir,
                    image_out_dir    = out_dir,
                    file_prefix      = sr.file_prefix,
                    start_frame      = start,
                    end_frame        = end,
                    sr               = sr,
                )
                generated.append(out)
                print(f"[SimpleRender] ✓ {out.name}")
            except Exception as e:
                errors.append(item.name)
                print(f"[SimpleRender] ERROR {item.name}: {e}")

        if errors:
            self.report({"WARNING"},
                f"{len(generated)} file(s) generated, {len(errors)} failed — see console.")
            return {"FINISHED"}

        # ── Success popup ─────────────────────────────────────────────────────
        def draw_popup(self_popup, context):
            col = self_popup.layout.column(align=True)
            col.label(text=f"  {len(generated)} .blend file(s) generated successfully!", icon="CHECKMARK")
            col.separator(factor=0.5)
            col.label(text="  Location:", icon="FILE_FOLDER")
            col.label(text=f"  {jobs_dir}")

        context.window_manager.popup_menu(
            draw_popup,
            title="SimpleRender — Generation Complete",
            icon="BLENDER",
        )

        # Also open folder in explorer
        try:
            if platform.system() == "Windows":
                os.startfile(str(jobs_dir))
            elif platform.system() == "Darwin":
                os.system(f'open "{jobs_dir}"')
            else:
                os.system(f'xdg-open "{jobs_dir}"')
        except Exception:
            pass

        return {"FINISHED"}


# ─────────────────────────────────────────────────────────────────────────────
#  UIList  —  aligned 3-column layout via split()
# ─────────────────────────────────────────────────────────────────────────────

class SR_UL_Collections(bpy.types.UIList):
    bl_idname = "SR_UL_collections"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        if self.layout_type not in {"DEFAULT", "COMPACT"}:
            layout.label(text="", icon="OUTLINER_COLLECTION")
            return

        # Mirror the exact same split factors as the header row in the panel
        split = layout.split(factor=COL_NAME_FACTOR, align=False)

        # ── Column 1 : Name ───────────────────────────────────────────────────
        split.label(text=item.name, icon="OUTLINER_COLLECTION")

        # ── Columns 2 & 3 : checkboxes ───────────────────────────────────────
        right = split.split(factor=COL_ALWAYS_FACTOR, align=False)
        right.prop(item, "always_render",  text="", emboss=True)
        right.prop(item, "one_time_render", text="", emboss=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Panel
# ─────────────────────────────────────────────────────────────────────────────

class SR_PT_MainPanel(bpy.types.Panel):
    bl_label       = "Simple Render"
    bl_idname      = "SR_PT_main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "Simple Render"
    bl_ui_units_x  = 14          # ~50 px wider than the default (~10 units)

    def draw_header(self, context):
        self.layout.label(icon="RENDER_STILL")

    def draw(self, context):
        layout      = self.layout
        sr          = context.scene.simple_render
        items       = context.scene.sr_collections
        scene       = context.scene
        blend_saved = bool(bpy.data.filepath)

        # ── Unsaved .blend warning ────────────────────────────────────────────
        if not blend_saved:
            box = layout.box()
            box.alert = True
            box.label(text="Save your .blend first!", icon="ERROR")
            layout.separator(factor=0.3)

        # ── Active Camera ─────────────────────────────────────────────────────
        box = layout.box()
        row = box.row()
        row.label(text="Active Camera", icon="CAMERA_DATA")
        if scene.camera:
            row.label(text=scene.camera.name)
        else:
            sub = row.row()
            sub.alert = True
            sub.label(text="None — set a camera!", icon="ERROR")

        # ── Output ────────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Output", icon="OUTPUT")
        box.prop(sr, "output_folder", text="Output Folder")
        box.prop(sr, "file_prefix",   text="Prefix")

        # Live preview
        example  = items[0].name if items else "MyAsset"
        frame_ex = sr.single_frame if sr.render_mode == "SINGLE" else sr.range_start
        sub = box.column()
        sub.scale_y = 0.7
        sub.label(text=f"  {sr.file_prefix}_{example}_{frame_ex:04d}.ext", icon="INFO")
        sub.label(text="  Jobs: Output Folder/_jobs/",                      icon="BLANK1")

        layout.separator(factor=0.5)

        # ── Frame Settings ────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Frame Settings", icon="TIME")
        row = box.row(align=True)
        row.prop(sr, "render_mode", expand=True)

        if sr.render_mode == "SINGLE":
            box.prop(sr, "single_frame", text="Frame Number")
        else:
            row = box.row(align=True)
            row.prop(sr, "range_start", text="Start")
            row.prop(sr, "range_end",   text="End")
            box.operator(SR_OT_GetBlenderRange.bl_idname,
                         text="Get Blender's Range", icon="SCENE_DATA")

        layout.separator(factor=0.5)

        # ── Image Sequence options ────────────────────────────────────────────
        box = layout.box()
        box.label(text="Image Sequence", icon="RENDER_ANIMATION")
        row = box.row(align=True)
        row.prop(sr, "use_overwrite")
        row.prop(sr, "use_placeholder")

        layout.separator(factor=0.5)

        # ── Collections ───────────────────────────────────────────────────────
        box    = layout.box()
        header = box.row()
        header.label(text="Collections", icon="OUTLINER_COLLECTION")
        header.operator(SR_OT_RefreshCollections.bl_idname,
                        text="", icon="FILE_REFRESH")

        if not items:
            sub = box.column()
            sub.scale_y = 0.8
            sub.label(text="No collections found.", icon="INFO")
            sub.label(text="Click  ↻  to scan the scene.", icon="BLANK1")
        else:
            # ── Column headers (split must match draw_item exactly) ───────────
            col_header = box.row(align=False)
            split = col_header.split(factor=COL_NAME_FACTOR, align=False)
            split.label(text="Name")
            right = split.split(factor=COL_ALWAYS_FACTOR, align=False)
            right.label(text="Always")
            right.label(text="One Time")

            # ── UIList ────────────────────────────────────────────────────────
            box.template_list(
                SR_UL_Collections.bl_idname, "",
                context.scene, "sr_collections",
                sr, "collection_index",
                rows=min(len(items), 6),
            )

            # ── Status ────────────────────────────────────────────────────────
            always_count   = sum(1 for i in items if i.always_render)
            one_time_count = sum(1 for i in items if i.one_time_render)

            col = box.column(align=True)
            col.scale_y = 0.75

            if always_count == 0:
                row = col.row()
                row.alert = True
                row.label(text="No Always Rendering — camera may be missing!", icon="ERROR")
            else:
                col.label(text=f"Always: {always_count}   One Time: {one_time_count}  →  {one_time_count} job(s)",
                          icon="CHECKMARK")

        layout.separator()

        # ── Action buttons ────────────────────────────────────────────────────
        can_act = (blend_saved and bool(items)
                   and any(i.one_time_render for i in items))

        # 1 — Launch Local Renders (BAT/SH with visible terminal)
        row = layout.row()
        row.enabled = can_act
        row.scale_y = 1.6
        row.operator(SR_OT_LaunchLocal.bl_idname,
                     text="  Launch Local Renders", icon="PLAY")

        # 2 — Generate Separate Blender Files
        row = layout.row()
        row.enabled = can_act
        row.scale_y = 1.3
        row.operator(SR_OT_GenerateBlendFiles.bl_idname,
                     text="  Generate Separate Blender Files", icon="FILE_BLEND")


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

CLASSES = (
    SR_CollectionItem,
    SimpleRenderProperties,
    SR_UL_Collections,
    SR_OT_RefreshCollections,
    SR_OT_GetBlenderRange,
    SR_OT_LaunchLocal,
    SR_OT_GenerateBlendFiles,
    SR_PT_MainPanel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.simple_render   = bpy.props.PointerProperty(type=SimpleRenderProperties)
    bpy.types.Scene.sr_collections  = bpy.props.CollectionProperty(type=SR_CollectionItem)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.simple_render
    del bpy.types.Scene.sr_collections


if __name__ == "__main__":
    register()
