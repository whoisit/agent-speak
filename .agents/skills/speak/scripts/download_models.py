"""Download the voice models into the repo (all gitignored).

    python download_models.py            Kokoro small model (88 MB) + voices (27 MB)
    python download_models.py --full     the full Kokoro model instead (325 MB)
    python download_models.py --piper    also Piper, the last-resort local fallback (~80 MB)

Files that are already there are skipped.
"""
import os
import sys
import shutil
import zipfile
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from speak import KOKORO_DIR, KOKORO_MODELS, PIPER_EXE, VOICE_MODEL

KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
PIPER_ZIP_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
PIPER_VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/en_US-joe-medium.onnx"


def download(url, dest):
    if os.path.exists(dest):
        print(f"Already there: {os.path.basename(dest)}")
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    def progress(blocks, block_size, total):
        done = blocks * block_size / 2**20
        size = f" of {total / 2**20:.0f}" if total > 0 else ""
        print(f"\r{os.path.basename(dest)}: {done:.0f}{size} MB", end="", flush=True)

    partial = dest + ".part"  # an interrupted download never looks complete
    urllib.request.urlretrieve(url, partial, progress)
    os.replace(partial, dest)
    print()


def main(args):
    model = KOKORO_MODELS["full" if "--full" in args else "small"]
    download(KOKORO_URL + model, os.path.join(KOKORO_DIR, model))
    download(KOKORO_URL + "voices-v1.0.bin", os.path.join(KOKORO_DIR, "voices-v1.0.bin"))
    if "--piper" in args:
        if os.path.exists(PIPER_EXE):
            print("Already there: piper")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                archive = os.path.join(tmp, "piper.zip")
                download(PIPER_ZIP_URL, archive)
                with zipfile.ZipFile(archive) as z:
                    z.extractall(tmp)
                shutil.copytree(os.path.join(tmp, "piper"), os.path.dirname(PIPER_EXE), dirs_exist_ok=True)
        download(PIPER_VOICE_URL, VOICE_MODEL)
        download(PIPER_VOICE_URL + ".json", VOICE_MODEL + ".json")
    if "--full" in args:
        print("Switch to it with: /speak model full")


if __name__ == "__main__":
    main(sys.argv[1:])
