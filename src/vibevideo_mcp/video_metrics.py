"""Deterministic output measurements without writing frame sequences to disk."""

import json
import subprocess
import sys
from pathlib import Path

from .io import digest, probe


def measure(path):
    import numpy as np

    info = probe(path)
    raw = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-i",
            str(path),
            "-an",
            "-map",
            "0:v:0",
            "-threads",
            "1",
            "-f",
            "framemd5",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=100,
    ).stdout.decode()
    hashes = [line.split(",")[-1].strip() for line in raw.splitlines() if line and not line.startswith("#")]
    longest, run, previous = 0, 0, None
    for value in hashes:
        run = run + 1 if value == previous else 1
        longest = max(longest, run)
        previous = value
    selection = f"select=eq(n\\,0)+eq(n\\,{len(hashes)-1}),format=rgb24"
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-i",
            str(path),
            "-an",
            "-vf",
            selection,
            "-vsync",
            "0",
            "-frames:v",
            "2",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    size = info["width"] * info["height"] * 3
    seam = None
    if len(hashes) > 1 and len(result) == size * 2:
        values = np.frombuffer(result, dtype=np.uint8).reshape(2, -1).astype(np.int16)
        delta = np.abs(values[0] - values[1])
        seam = {
            "mean_absolute_channel_difference_0_255": float(delta.mean()),
            "fraction_channels_difference_gt_8": float((delta > 8).mean()),
            "mean_absolute_channel_difference_0_1": float(delta.mean() / 255),
        }
    return {
        "source_sha256": digest(path),
        "info": info,
        "decoded_frame_count": len(hashes),
        "longest_identical_frame_run": longest,
        "longest_identical_frame_run_seconds": longest / info["fps"],
        "first_last_rgb_seam": seam,
        "provider_called": False,
        "limitations": "Identical decoded-frame hashes do not measure semantic subject motion; animated noise can change hashes. Seam measures endpoints, not continuity of the surrounding motion.",
    }


if __name__ == "__main__":
    print(json.dumps(measure(Path(sys.argv[1]))))
