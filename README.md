# Blender Render by EMF

Renders your scene on [Modal](https://modal.com)'s free GPU cloud instead of
your local machine. Free tier gives $30/month in compute credits with no
credit card required - roughly 50 hours on a T4 GPU. Built for personal
hobby use.

Maintained by EMF - [github.com/emf187](https://github.com/emf187). Issues
and PRs welcome there.

## Why Modal instead of Kaggle

This addon originally targeted Kaggle, but Kaggle's dataset -> kernel ->
blob-upload flow turned out to be fragile to automate (phone verification
requirements, multi-step auth, opaque 401s). Modal's Python SDK is a much
better fit for driving from inside an addon: one token, a single function
call to run remotely, and file transfer built into the SDK - no separate
"upload a dataset" step.

## One-time setup

1. **Modal account + token**
   - Sign up at [modal.com](https://modal.com) (no credit card needed for
     the free tier).
   - Go to modal.com/settings/tokens and create a new token. You'll get a
     Token ID and Token Secret.

2. **Install the addon**
   - Zip the `blender_render_by_emf/` folder and install it via
     Blender > Edit > Preferences > Add-ons > Install.
   - Enable "Blender Render by EMF".

3. **Enter credentials + deploy**
   - In addon preferences, paste your Modal Token ID and Token Secret.
   - Click "Install/Check Modal Package" (installs the `modal` package into
     Blender's bundled Python - only needed once).
   - Click "Deploy / Update Modal App". First deploy builds a container
     image with Blender installed, which takes a couple of minutes. After
     that, renders reuse the already-deployed app and start immediately.

## Using it

1. Save your `.blend` file.
2. Open Properties > Render tab, find the "Blender Render by EMF" panel.
3. Pick CUDA or OptiX, click "Render on Modal".
4. The panel shows live status: uploading -> running -> complete.
5. When done, the rendered frame(s) are downloaded locally AND automatically
   loaded as an Image node in your Compositor - if a "Compositing"
   workspace exists it'll switch you there, plus you'll get a popup
   confirming where the files landed on disk (under
   `~/.blender_modal_render/downloads/<timestamp>/`).

## Terminating a stuck job

Click "Terminate Job" in the panel - this stops Blender from tracking the
job locally (works even if the network hangs) and also requests
cancellation on Modal's side, so you don't keep burning GPU-hours on a job
you've abandoned.

## Known limitations

- **External/linked assets**: only the `.blend` file itself is sent. Pack
  external textures first (File > External Data > Pack Resources) or the
  Modal-side render will be missing them.
- **Blender version is fixed at deploy time**: `modal_app_script.py` has a
  `BLENDER_VERSION` constant baked into the container image. If you're on a
  different Blender version, update it and click "Deploy / Update Modal
  App" again.
- **GPU type is fixed at deploy time too**: change `GPU_TYPE` in
  `modal_app_script.py` (T4 / A10G / L4 / L40S / A100 / H100) and redeploy
  if you want something other than T4. T4 is the default because it
  stretches the free $30/month credit furthest (~50 hours).
- **$30/month free credit isn't unlimited** - fine for stills and short
  animations; long heavy animations may need to be split across frame
  ranges or months.

## Possible next steps

- Job queue for submitting several frame ranges back-to-back.
- Auto-detect and pack external texture files instead of requiring manual
  packing.
- Live render progress/log streaming instead of just a status string.
