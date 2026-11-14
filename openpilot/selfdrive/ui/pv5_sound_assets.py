"""PV5-only official tones. File I/O occurs in the existing startup/language load."""

from pathlib import Path
import wave

import numpy as np

OFFICIAL_SOUND_FILES = {
  'engage': 'engage.wav',
  'disengage': 'disengage.wav',
  'refuse': 'refuse.wav',
  'prompt': 'warning.wav',
  'promptRepeat': 'warning.wav',
  'promptDistracted': 'dm_warning.wav',
  'warningSoft': 'critical.wav',
  'warningImmediate': 'dm_critical.wav',
  # Carrot's separate cruise-state event IDs keep their existing play counts.
  'longEngaged': 'engage.wav',
  'longDisengaged': 'disengage.wav',
  'engage2': 'engage.wav',
  'disengage2': 'disengage.wav',
}


def load_official_sound_pack(directory, sound_list, alerts, legacy_sounds, legacy_gains):
  """Build a complete profile before publishing it; retain every legacy fallback."""
  sounds, gains = legacy_sounds.copy(), legacy_gains.copy()
  raw, scaled = {}, {}
  for name, filename in OFFICIAL_SOUND_FILES.items():
    alert = getattr(alerts, name)
    if filename not in raw:
      with wave.open(str(Path(directory) / filename), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 48000):
          raise ValueError(f'Invalid official sound format: {filename}')
        frames = wav.getnframes()
        if not 0 < frames <= 96000:
          raise ValueError(f'Invalid official sound length: {filename}')
        data = wav.readframes(frames)
        if len(data) != frames * 2:
          raise ValueError(f'Truncated official sound: {filename}')
        raw[filename] = np.frombuffer(data, dtype=np.int16)
    volume = sound_list[alert][2]
    key = filename, volume
    if key not in scaled:
      scaled[key] = raw[filename].astype(np.float32) * (volume / 32768.0)
    sounds[alert] = scaled[key]
    peak = float(np.max(np.abs(sounds[alert]), initial=0.0))
    gains[alert] = 1.0 / peak if 0.0 < peak < 1.0 else 1.0
  return sounds, gains
