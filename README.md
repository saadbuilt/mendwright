# mendwright explainer video

This branch holds the source of the explainer video in the main README, and nothing else.
It is a deterministic HTML composition: `video/compose.js` draws the frame at time *t*, and
`video/render.py` captures every frame with headless Chromium and encodes them with ffmpeg.

Every selector, log line, error and timing in the video comes from `video/capture.json`,
real runs of mendwright against the demo shop pages in `video/pages.json`. Waits that are
sped up in the video are labelled as sped up.

The video is rendered by GitHub Actions on every push to this branch
(`.github/workflows/render-video.yml`): ten jobs render a part each, and a last job joins
them. Download it from the run's `mendwright-explainer` artifact.

To render locally:

```bash
pip install playwright==1.53.0 && python -m playwright install chromium
python video/render.py --stills 2 20 45     # a few frames
python video/render.py --video out.mp4      # the whole video (needs ffmpeg)
```
