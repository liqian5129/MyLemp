import sys
import sounddevice as sd
import numpy as np


def get_audio_devices():
    """Return (output_device, input_device) indices based on platform."""
    if sys.platform == "darwin":
        # On macOS, use system default devices (built-in speaker & microphone)
        return None, None

    # On Linux / Raspberry Pi, look for Seeed USB audio device
    seeed_output = None
    seeed_input = None
    for i, d in enumerate(sd.query_devices()):
        if "seeed" not in d['name'].lower():
            continue
        if seeed_output is None and d['max_output_channels'] > 0:
            seeed_output = i
        if seeed_input is None and d['max_input_channels'] > 0:
            seeed_input = i

    if seeed_output is None or seeed_input is None:
        raise RuntimeError("Seeed device not found!")
    return seeed_output, seeed_input


output_dev, input_dev = get_audio_devices()

# --- Test Speaker ---
duration = 3  # seconds
sample_rate = 44100  # Hz

print("Playing test tone...")
frequency = 440  # Hz (A4 note)
t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
tone = 0.5 * np.sin(2 * np.pi * frequency * t)
sd.play(tone, samplerate=sample_rate, device=output_dev)
sd.wait()

# --- Test Microphone ---
print("Recording from microphone...")
recording = sd.rec(int(duration * sample_rate), samplerate=sample_rate,
                   channels=1, device=input_dev)
sd.wait()
print("Recording complete.")

# --- Playback Recorded Audio ---
print("Playing back recorded audio...")
sd.play(recording, samplerate=sample_rate, device=output_dev)
sd.wait()
print("Done.")
