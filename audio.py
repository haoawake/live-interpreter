"""Audio capture: system audio or a microphone, as (sample_rate, mono float32) blocks.

The platform code lives in audio_win.py (WASAPI) and audio_mac.py (Core Audio
through the bundled lt-audio helper); both export the same names.
"""
import sys

__all__ = ["MIC_DEFAULT", "SYSTEM_DEFAULT", "Capture", "list_sources"]

if sys.platform == "darwin":
    from audio_mac import MIC_DEFAULT, SYSTEM_DEFAULT, Capture, list_sources  # noqa: F401
else:
    from audio_win import MIC_DEFAULT, SYSTEM_DEFAULT, Capture, list_sources  # noqa: F401
