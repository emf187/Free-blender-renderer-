"""
This is the actual application that runs on Modal's servers. It gets
deployed once via `python -m modal deploy modal_app_script.py` (the addon's
"Deploy / Update Modal App" button does this for you) and then stays live -
each render just calls the already-deployed `render` function, it does NOT
redeploy every time.

If you want a different Blender version or GPU type, edit the constants
below and click "Deploy / Update Modal App" again in the addon preferences.
"""

import os
import glob
import subprocess

import modal

APP_NAME = "blender-render-by-emf"

# Match this to your local Blender version where possible - newer Blender
# can usually open older .blend files, but not reliably the other way
# around.
BLENDER_VERSION = "5.0.1"
BLENDER_URL = f"https://download.blender.org/release/Blender5.0/blender-{BLENDER_VERSION}-linux-x64.tar.xz"

# T4 gives the best hours-per-dollar on Modal's free $30/month credit
# (~50 hours). Other options: "A10G", "L4", "L40S", "A100", "H100".
GPU_TYPE = "T4"

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "wget",
        "xz-utils",
        "libgl1",
        "libxi6",
        "libxrender1",
        "libxfixes3",
        "libxkbcommon0",
        "libsm6",
        "libxext6",
        "libxcb1",
    )
    .run_commands(
        f"wget -q -O /tmp/blender.tar.xz {BLENDER_URL}",
        "mkdir -p /opt/blender",
        "tar -xf /tmp/blender.tar.xz -C /opt/blender --strip-components=1",
        "rm /tmp/blender.tar.xz",
    )
)


@app.function(image=image, gpu=GPU_TYPE, timeout=60 * 60 * 2)
def render(
    blend_bytes: bytes,
    engine: str = "CYCLES",
    animation: bool = False,
    start_frame: int = 1,
    end_frame: int = 1,
    output_format: str = "PNG",
    device_type: str = "CUDA",
) -> dict:
    work_dir = "/tmp/work"
    os.makedirs(work_dir, exist_ok=True)
    blend_path = os.path.join(work_dir, "scene.blend")
    with open(blend_path, "wb") as f:
        f.write(blend_bytes)

    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    device_script = os.path.join(work_dir, "device_setup.py")
    with open(device_script, "w") as f:
        f.write(f'''
import bpy
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "{device_type}"
    prefs.get_devices()
    for d in prefs.devices:
        d.use = True
    bpy.context.scene.cycles.device = "GPU"
    print("[modal-render] GPU device configured:", prefs.compute_device_type)
except Exception as e:
    print("[modal-render] GPU device setup skipped/failed:", e)
''')

    output_pattern = os.path.join(output_dir, "frame_#####")
    cmd = [
        "/opt/blender/blender", "-b", blend_path,
        "-noaudio", "-E", engine,
        "-P", device_script,
        "-o", output_pattern, "-F", output_format,
    ]
    if animation:
        cmd += ["-s", str(start_frame), "-e", str(end_frame), "-a"]
    else:
        cmd += ["-f", str(start_frame)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    log = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:
        raise RuntimeError(f"Blender exited with code {result.returncode}:\n{log[-4000:]}")

    files = {}
    for path in sorted(glob.glob(os.path.join(output_dir, "*"))):
        with open(path, "rb") as f:
            files[os.path.basename(path)] = f.read()

    if not files:
        raise RuntimeError("Blender finished but produced no output files.\n" + log[-2000:])

    return {"files": files, "log": log[-2000:]}
