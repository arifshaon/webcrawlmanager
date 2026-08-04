"""Simple Webcrawl Manager (SWM) — browser-based web archiving to WARC."""

__version__ = "0.2.0"

# Install the hardened interactive-recording runtime before CLI or dashboard
# callers import RecordingSession from webarc.recorder.
from .recording_runtime import install as _install_recording_runtime

_install_recording_runtime()
del _install_recording_runtime
