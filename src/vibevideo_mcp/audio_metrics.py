"""Only audio can cross the external perception boundary. No visual payloads."""

import json
import subprocess


def measure(path):
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-threads",
            "1",
            "-i",
            str(path),
            "-vn",
            "-af",
            "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=80,
    )
    text = result.stderr
    start, end = text.rfind("{"), text.rfind("}")
    if result.returncode or start < 0:
        return {
            "measured": False,
            "reason": "No analyzable audio stream or ffmpeg measurement failed",
            "provider_called": False,
        }
    data = json.loads(text[start : end + 1])
    return {
        "measured": True,
        "provider_called": False,
        "integrated_lufs": data["input_i"],
        "true_peak_dbtp": data["input_tp"],
        "loudness_range_lu": data["input_lra"],
        "threshold_lufs": data["input_thresh"],
        "note": "Measurements of decoded input audio, not a semantic or musical-quality judgment",
    }
