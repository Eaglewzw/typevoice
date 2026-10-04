#!/usr/bin/env python3
"""Regenerate matching recording cues using only the Python standard library.

The bundled WAV is played directly; synthesis never runs on the hotkey path.
The stop cue uses the same tone at roughly +9.5 dB for clearer end feedback.
Run from any directory: python3 scripts/generate_cue_sounds.py
"""

import math
from pathlib import Path
import struct
import wave


SAMPLE_RATE = 48000
DURATION = 0.140
FREQUENCY = 660.0
START_PEAK = 0.11
STOP_GAIN = 3.0
ATTACK = 0.010
RELEASE = 0.045
DECAY = 0.055
ASSETS = Path(__file__).resolve().parents[1] / "assets"


def main():
    count = round(SAMPLE_RATE * DURATION)
    end = (count - 1) / SAMPLE_RATE
    samples = []
    for index in range(count):
        time = index / SAMPLE_RATE
        # Smooth edges prevent clicks; exponential decay gives a soft pluck.
        attack = math.sin(math.pi / 2 * min(time / ATTACK, 1.0)) ** 2
        release = math.sin(math.pi / 2 * min((end - time) / RELEASE, 1.0)) ** 2
        envelope = attack * release * math.exp(-time / DECAY)
        phase = 2 * math.pi * FREQUENCY * time
        # A faint octave adds warmth without a bright, sweeping electronic beep.
        tone = math.sin(phase) + 0.06 * math.sin(2 * phase) * math.exp(-time / 0.030)
        samples.append(envelope * tone)

    scale = START_PEAK * 32767 / max(abs(sample) for sample in samples)
    for name, gain in (("start", 1.0), ("stop", STOP_GAIN)):
        pcm = struct.pack(f"<{count}h", *(round(sample * scale * gain) for sample in samples))
        path = ASSETS / f"record-{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            output.writeframes(pcm)
        print(path)


if __name__ == "__main__":
    main()
