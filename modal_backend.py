"""
Thin wrapper around the official `modal` PyPI package.

Same reasoning as before: Blender's bundled Python has user site-packages
disabled, so `pip install --user modal` would succeed but stay invisible to
Blender. Instead we install into a `lib/` folder sitting right inside this
addon and add it to sys.path ourselves.

None of these functions import bpy - safe to test outside Blender.
"""

import os
import sys
import glob
import shutil
import subprocess
import socket
import threading
import time

# Bound how long any single blocking network call can hang for, so a flaky
# connection can't freeze the worker thread indefinitely.
socket.setdefaulttimeout(30)

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(ADDON_DIR, "lib")
APP_SCRIPT = os.path.join(ADDON_DIR, "modal_app_script.py")

APP_NAME = "blender-render-by-emf"
FUNCTION_NAME = "render"

ADDON_DATA_DIR = os.path.join(os.path.expanduser("~"), ".blender_modal_render")
DOWNLOAD_DIR = os.path.join(ADDON_DATA_DIR, "downloads")

# Set by the addon's Terminate button; checked between polls in
# wait_for_completion() so a stuck job can be abandoned without closing
# Blender.
cancel_event = threading.Event()


class AppNotDeployedError(Exception):
    """Raised when the render function hasn't been deployed to Modal yet."""
    pass


def ensure_dirs():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(LIB_DIR, exist_ok=True)


def ensure_importable():
    ensure_dirs()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)


def _diagnostic(extra=""):
    lines = [
        "Modal package diagnostic:",
        f"  python executable: {sys.executable}",
        f"  LIB_DIR: {LIB_DIR}",
        f"  LIB_DIR exists: {os.path.isdir(LIB_DIR)}",
        f"  LIB_DIR contents (first 20): {os.listdir(LIB_DIR)[:20] if os.path.isdir(LIB_DIR) else 'n/a'}",
        f"  sys.path[0:3]: {sys.path[:3]}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def ensure_pip():
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        try:
            import ensurepip
            ensurepip.bootstrap()
            return True
        except Exception:
            return False


def ensure_modal_installed():
    ensure_importable()
    try:
        import modal
        return True, f"modal package already available (v{modal.__version__})"
    except ImportError:
        pass

    if not ensure_pip():
        return False, "pip is not available in Blender's Python and could not be bootstrapped."

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--target", LIB_DIR, "--upgrade", "modal"]
        )
    except subprocess.CalledProcessError as e:
        return False, f"pip install failed: {e}\n\n{_diagnostic()}"

    ensure_importable()
    try:
        import importlib
        import modal
        importlib.reload(modal)
        return True, f"modal package installed successfully (v{modal.__version__})"
    except ImportError as e:
        return False, f"Installed but still not importable: {e}\n\n{_diagnostic()}"


def write_credentials(token_id, token_secret):
    """
    Modal reads MODAL_TOKEN_ID / MODAL_TOKEN_SECRET from the environment -
    no config file needed, which sidesteps the whole class of "file wasn't
    where it expected" problems we hit with Kaggle.
    """
    os.environ["MODAL_TOKEN_ID"] = (token_id or "").strip()
    os.environ["MODAL_TOKEN_SECRET"] = (token_secret or "").strip()


def deploy_app():
    """
    Deploys/updates the render function on Modal. Builds the container
    image (installs Blender etc.) the first time, which can take a couple
    of minutes; after that, redeploys are fast unless you changed
    modal_app_script.py.
    """
    ensure_importable()
    env = os.environ.copy()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modal", "deploy", APP_SCRIPT],
            capture_output=True, text=True, env=env, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "Deploy timed out after 15 minutes."

    log = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        return False, log[-4000:]
    return True, log[-2000:]


def spawn_render(blend_path, engine, animation, start_frame, end_frame, output_format, device_type):
    ensure_importable()
    import modal
    from modal.exception import NotFoundError

    with open(blend_path, "rb") as f:
        blend_bytes = f.read()

    try:
        fn = modal.Function.from_name(APP_NAME, FUNCTION_NAME)
        call = fn.spawn(
            blend_bytes=blend_bytes,
            engine=engine,
            animation=animation,
            start_frame=start_frame,
            end_frame=end_frame,
            output_format=output_format,
            device_type=device_type,
        )
    except NotFoundError:
        raise AppNotDeployedError(
            f"'{APP_NAME}' isn't deployed on Modal yet. "
            "Click 'Deploy / Update Modal App' in addon preferences first."
        )
    return call.object_id


def poll_render(call_id, poll_timeout=8):
    """
    Blocks up to poll_timeout seconds waiting for the result.
    Returns ('running', None) or ('complete', result_dict).
    """
    ensure_importable()
    import modal
    from modal.exception import TimeoutError as ModalTimeoutError

    call = modal.FunctionCall.from_id(call_id)
    try:
        result = call.get(timeout=poll_timeout)
        return "complete", result
    except ModalTimeoutError:
        return "running", None


def cancel_render(call_id):
    ensure_importable()
    import modal
    call = modal.FunctionCall.from_id(call_id)
    call.cancel()


def wait_for_completion(call_id, poll_interval=10, timeout=60 * 60 * 3, on_update=None):
    start = time.time()
    while time.time() - start < timeout:
        if cancel_event.is_set():
            try:
                cancel_render(call_id)
            except Exception:
                pass
            return "terminated", None
        status, result = poll_render(call_id, poll_timeout=min(poll_interval, 15))
        if on_update:
            on_update(status)
        if status == "complete":
            return "complete", result
    return "timeout", None


def save_output(result, dest_dir=None):
    ensure_dirs()
    dest_dir = dest_dir or os.path.join(DOWNLOAD_DIR, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(dest_dir, exist_ok=True)
    saved_paths = []
    for name, data in sorted(result.get("files", {}).items()):
        path = os.path.join(dest_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        saved_paths.append(path)
    return dest_dir, saved_paths
