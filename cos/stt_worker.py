"""Speech-to-text for one audio file. Runs in the voice venv (see cos/voice.py), not the bot's.

    <voice venv python> -I cos/stt_worker.py <mlx model repo> <audio file>

Prints the transcript on stdout. ffmpeg comes from imageio-ffmpeg, so nothing needs Homebrew.
"""

import subprocess
import sys

import imageio_ffmpeg
import mlx_whisper
import numpy as np


def main() -> int:
    model, path = sys.argv[1], sys.argv[2]
    # Decode anything Slack sends (webm/opus, mp4/aac, mp3...) to 16 kHz mono PCM.
    raw = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", path,
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model)
    print(result["text"].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
