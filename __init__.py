bl_info = {
    "name": "Blender Render by EMF",
    "author": "EMF",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "Render Properties > Blender Render by EMF",
    "description": "Render your scene on Modal's free GPU cloud instead of locally",
    "category": "Render",
}

import bpy
import os
import threading
import traceback

from . import modal_backend

GITHUB_URL = "https://github.com/emf187"

# Shared state for the background worker thread <-> modal operator.
_job_state = {
    "status": "idle",       # idle | deploying | uploading | queued | running | complete | error | terminated
    "message": "",
    "call_id": None,
    "output_dir": None,
    "saved_paths": None,
    "animation": False,
}


class ModalRenderPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    token_id: bpy.props.StringProperty(name="Modal Token ID")
    token_secret: bpy.props.StringProperty(name="Modal Token Secret", subtype="PASSWORD")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "token_id")
        layout.prop(self, "token_secret")
        layout.label(text="Get these from modal.com/settings/tokens", icon="INFO")

        row = layout.row(align=True)
        row.operator("modal_render.install_deps", icon="IMPORT")
        row.operator("modal_render.deploy_app", icon="URL")

        box = layout.box()
        box.label(text="Blender Render by EMF", icon="RENDER_RESULT")
        op = box.operator("wm.url_open", text="github.com/emf187", icon="URL")
        op.url = GITHUB_URL


class ModalRenderSettings(bpy.types.PropertyGroup):
    device_type: bpy.props.EnumProperty(
        name="GPU Type",
        items=[("CUDA", "CUDA (safe default)", ""), ("OPTIX", "OptiX (RT-capable)", "")],
        default="CUDA",
    )
    poll_interval: bpy.props.IntProperty(name="Poll Interval (s)", default=10, min=5, max=60)


class MODAL_RENDER_OT_install_deps(bpy.types.Operator):
    bl_idname = "modal_render.install_deps"
    bl_label = "Install/Check Modal Package"

    def execute(self, context):
        ok, msg = modal_backend.ensure_modal_installed()
        level = "INFO" if ok else "ERROR"
        self.report({level}, msg)
        return {"FINISHED" if ok else "CANCELLED"}


class MODAL_RENDER_OT_deploy_app(bpy.types.Operator):
    bl_idname = "modal_render.deploy_app"
    bl_label = "Deploy / Update Modal App"
    bl_description = (
        "Deploys the render function to Modal. Needed once before your "
        "first render, and again any time you edit modal_app_script.py "
        "(e.g. to change GPU type or Blender version)"
    )

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        if not prefs.token_id or not prefs.token_secret:
            self.report({"ERROR"}, "Set your Modal token ID + secret in addon preferences first")
            return {"CANCELLED"}

        ok, msg = modal_backend.ensure_modal_installed()
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        modal_backend.write_credentials(prefs.token_id, prefs.token_secret)

        self.report({"INFO"}, "Deploying to Modal - this can take a couple of minutes the first time...")
        ok, log = modal_backend.deploy_app()
        if ok:
            self.report({"INFO"}, "Deployed successfully")
        else:
            self.report({"ERROR"}, log[:2000])
        return {"FINISHED" if ok else "CANCELLED"}


def show_popup(title, message, icon="INFO"):
    def draw(self, context):
        for line in message.split("\n"):
            self.layout.label(text=line[:100])
    bpy.context.window_manager.popup_menu(draw, title=title, icon=icon)


def load_result_into_compositor(context, saved_paths, animation):
    """
    Loads the rendered frame(s) as an Image node in the Compositor, next to
    whatever's already there, and switches to the Compositing workspace if
    one exists so the result is immediately visible - this all has to run
    on Blender's main thread (bpy is not thread-safe), so it's called from
    the modal operator, never from the background worker thread.
    """
    if not saved_paths:
        return None

    scene = context.scene
    scene.use_nodes = True
    tree = scene.node_tree

    first_path = sorted(saved_paths)[0]
    img = bpy.data.images.load(first_path, check_existing=False)

    max_x = 0.0
    for n in tree.nodes:
        max_x = max(max_x, n.location.x + 220)

    node = tree.nodes.new(type="CompositorNodeImage")
    node.image = img
    node.location = (max_x + 60, 0)
    node.label = "EMF Modal Render Result"

    if animation and len(saved_paths) > 1:
        img.source = "SEQUENCE"
        node.frame_duration = len(saved_paths)

    for n in tree.nodes:
        n.select = False
    node.select = True
    tree.nodes.active = node

    for ws in bpy.data.workspaces:
        if ws.name == "Compositing":
            context.window.workspace = ws
            break

    for area in context.screen.areas:
        area.tag_redraw()

    return node


class MODAL_RENDER_OT_submit(bpy.types.Operator):
    bl_idname = "modal_render.submit"
    bl_label = "Render on Modal"

    _timer = None
    _thread = None

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        settings = context.scene.modal_render_settings

        if not prefs.token_id or not prefs.token_secret:
            self.report({"ERROR"}, "Set your Modal token ID + secret in addon preferences first")
            return {"CANCELLED"}

        blend_path = bpy.data.filepath
        if not blend_path:
            self.report({"ERROR"}, "Save your .blend file first")
            return {"CANCELLED"}

        scene = context.scene
        job = {
            "token_id": prefs.token_id,
            "token_secret": prefs.token_secret,
            "blend_path": blend_path,
            "engine": scene.render.engine,
            "animation": scene.frame_start != scene.frame_end,
            "start_frame": scene.frame_start,
            "end_frame": scene.frame_end,
            "output_format": scene.render.image_settings.file_format,
            "device_type": settings.device_type,
            "poll_interval": settings.poll_interval,
        }

        _job_state["status"] = "uploading"
        _job_state["message"] = "Sending scene to Modal..."
        _job_state["saved_paths"] = None
        _job_state["output_dir"] = None

        self._thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        self._thread.start()

        self._timer = context.window_manager.event_timer_add(1.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "TIMER":
            for area in context.screen.areas:
                area.tag_redraw()

            if _job_state["status"] in ("complete", "error", "terminated"):
                context.window_manager.event_timer_remove(self._timer)

                if _job_state["status"] == "complete":
                    self.report({"INFO"}, f"Done. Frames in {_job_state['output_dir']}")
                    node = load_result_into_compositor(
                        context, _job_state["saved_paths"], _job_state["animation"]
                    )
                    show_popup(
                        "Render complete - Blender Render by EMF",
                        f"{len(_job_state['saved_paths'] or [])} frame(s) saved to:\n"
                        f"{_job_state['output_dir']}\n\n"
                        "Added to the Compositor node tree - ready to composite.",
                    )
                elif _job_state["status"] == "terminated":
                    self.report({"WARNING"}, _job_state["message"])
                else:
                    self.report({"ERROR"}, _job_state["message"])
                    show_popup("Render failed - Blender Render by EMF", _job_state["message"], icon="ERROR")

                return {"FINISHED"}
        return {"PASS_THROUGH"}

    @staticmethod
    def _run_job(job):
        try:
            modal_backend.cancel_event.clear()
            modal_backend.write_credentials(job["token_id"], job["token_secret"])

            ok, msg = modal_backend.ensure_modal_installed()
            if not ok:
                _job_state["status"] = "error"
                _job_state["message"] = msg[:4000]
                return

            if modal_backend.cancel_event.is_set():
                _job_state["status"] = "terminated"
                _job_state["message"] = "Terminated before submitting"
                return

            _job_state["status"] = "uploading"
            try:
                call_id = modal_backend.spawn_render(
                    job["blend_path"],
                    job["engine"],
                    job["animation"],
                    job["start_frame"],
                    job["end_frame"],
                    job["output_format"],
                    job["device_type"],
                )
            except modal_backend.AppNotDeployedError:
                _job_state["status"] = "deploying"
                _job_state["message"] = "First run: deploying render app to Modal (~1-3 min)..."
                ok, log = modal_backend.deploy_app()
                if not ok:
                    _job_state["status"] = "error"
                    _job_state["message"] = f"Deploy failed:\n{log}"
                    return
                call_id = modal_backend.spawn_render(
                    job["blend_path"],
                    job["engine"],
                    job["animation"],
                    job["start_frame"],
                    job["end_frame"],
                    job["output_format"],
                    job["device_type"],
                )

            _job_state["call_id"] = call_id
            _job_state["status"] = "running"

            def on_update(status):
                _job_state["status"] = status
                _job_state["message"] = f"Modal job status: {status}"

            final_status, result = modal_backend.wait_for_completion(
                call_id, poll_interval=job["poll_interval"], on_update=on_update
            )

            if final_status == "terminated":
                _job_state["status"] = "terminated"
                _job_state["message"] = (
                    f"Terminated. Requested cancellation of the Modal job ({call_id}) "
                    "as well - check modal.com if you want to confirm it stopped."
                )
                return

            if final_status != "complete":
                _job_state["status"] = "error"
                _job_state["message"] = f"Job ended with status: {final_status}"
                return

            out_dir, saved_paths = modal_backend.save_output(result)
            _job_state["output_dir"] = out_dir
            _job_state["saved_paths"] = saved_paths
            _job_state["animation"] = job["animation"]
            _job_state["status"] = "complete"
            _job_state["message"] = "Done"

        except Exception:
            _job_state["status"] = "error"
            _job_state["message"] = traceback.format_exc(limit=3)


class MODAL_RENDER_OT_terminate(bpy.types.Operator):
    bl_idname = "modal_render.terminate"
    bl_label = "Terminate Job"
    bl_description = (
        "Stop tracking the current job locally without closing Blender, "
        "and request cancellation on Modal's side too"
    )

    def execute(self, context):
        if _job_state["status"] in ("idle", "complete", "error", "terminated"):
            self.report({"INFO"}, "No active job to terminate")
            return {"CANCELLED"}
        modal_backend.cancel_event.set()
        self.report({"INFO"}, "Terminate requested, wrapping up current step...")
        return {"FINISHED"}


class MODAL_RENDER_PT_panel(bpy.types.Panel):
    bl_label = "Blender Render by EMF"
    bl_idname = "MODAL_RENDER_PT_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.modal_render_settings

        layout.prop(settings, "device_type")
        layout.prop(settings, "poll_interval")

        status = _job_state["status"]
        job_active = status not in ("idle", "complete", "error", "terminated")

        row = layout.row()
        row.enabled = not job_active
        row.operator("modal_render.submit", icon="RENDER_ANIMATION")

        if job_active:
            term_row = layout.row()
            term_row.alert = True
            term_row.operator("modal_render.terminate", icon="CANCEL")

        if status != "idle":
            box = layout.box()
            box.label(text=f"Status: {status}")
            if _job_state["message"]:
                for line in _job_state["message"][:400].split("\n")[:4]:
                    box.label(text=line[:80])
            if status == "complete":
                box.label(text=f"Output: {_job_state['output_dir']}", icon="FILE_FOLDER")

        layout.separator()
        op = layout.operator("wm.url_open", text="Maintained by EMF - github.com/emf187", icon="URL")
        op.url = GITHUB_URL


classes = (
    ModalRenderPreferences,
    ModalRenderSettings,
    MODAL_RENDER_OT_install_deps,
    MODAL_RENDER_OT_deploy_app,
    MODAL_RENDER_OT_submit,
    MODAL_RENDER_OT_terminate,
    MODAL_RENDER_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.modal_render_settings = bpy.props.PointerProperty(type=ModalRenderSettings)


def unregister():
    del bpy.types.Scene.modal_render_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
