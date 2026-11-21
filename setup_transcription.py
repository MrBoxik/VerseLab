"""Verify the pinned Large-v3 weights and build the local Vulkan worker."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import urllib.request

from transcription import FIXED_MODEL, MODELS

BASE = Path(__file__).resolve().parent
REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"


def checksum(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=[FIXED_MODEL, "all"], default=FIXED_MODEL)
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    spec = MODELS[FIXED_MODEL]
    target = BASE / "models" / spec["file"]
    target.parent.mkdir(exist_ok=True)
    if not target.is_file() or checksum(target) != spec["sha256"]:
        partial = target.with_suffix(".part")
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{REVISION}/{target.name}"
        urllib.request.urlretrieve(url, partial)
        if checksum(partial) != spec["sha256"]:
            partial.unlink(missing_ok=True)
            raise ValueError("Large-v3 checksum mismatch")
        partial.replace(target)
    if args.download_only:
        print("Whisper Large-v3 model is ready.", flush=True)
        return
    env = {**os.environ, "DOTNET_CLI_HOME": str(BASE / "runtime" / "dotnet-home"),
           "NUGET_PACKAGES": str(BASE / "runtime" / "nuget"), "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}
    project = BASE / "experiments" / "large-model" / "LargeModelBenchmark.csproj"
    subprocess.run(["dotnet", "restore", str(project), "--locked-mode"], env=env, check=True)
    subprocess.run(["dotnet", "build", str(project), "-c", "Release", "--no-restore", "-o",
                    str(BASE / "runtime" / "lyrics-gpu")], env=env, check=True)
    print("Whisper Large-v3 Vulkan GPU worker ready.")


if __name__ == "__main__":
    main()
