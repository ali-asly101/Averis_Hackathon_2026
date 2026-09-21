"""
Deploy the whole app (UI + API) to a Hugging Face Space - free, public URL.

    python deploy/deploy_hf.py              # build folder + upload
    python deploy/deploy_hf.py --dry-run    # only build deploy_build/ and check it

Needs in .env:
    HF_TOKEN=hf_...        a *write* token from https://huggingface.co/settings/tokens
    HF_SPACE=user/name     e.g. alioamr/cargosense  (created if it doesn't exist)
    LLM_API_KEY=...        stored as an encrypted Space secret, never in files

What gets uploaded (deploy_build/):
    sdoc/, frontend/ (source; the Space builds it), requirements.txt, Dockerfile,
    .dockerignore, a Space README, and data/ = inbox/ + attachments/ +
    sample_submission.json from your data folder. ground_truth.json is NEVER
    copied, and the script refuses to upload if one is found.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sdoc import config  # noqa: E402  (also loads .env)

BUILD = ROOT / "deploy_build"
SPACE_README = """---
title: CargoSense - Shipping Document Verification
emoji: 🚢
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# CargoSense - Shipping Document Verification

Averis x Monash Hackathon 2026. Email inbox -> classify -> extract SI / BL fields
(txt, PDF, Word, Excel, scans) -> compare -> discrepancy report, with a human
review queue. Try **Upload & Check** with your own SI and BL documents.
"""
SECRETS = ["LLM_API_KEY", "SDOC_ADMIN_TOKEN"]
VARIABLES = ["LLM_BASE_URL", "LLM_MODEL", "LLM_VISION_MODEL", "SDOC_EXTRACTION_MODE",
             "SDOC_OCR_POLICY", "SDOC_RUN_COOLDOWN", "SDOC_CHECKS_PER_HOUR"]


def build(data_dir):
    data_dir = Path(data_dir)
    if not (data_dir / "inbox").is_dir() or not (data_dir / "attachments").is_dir():
        sys.exit(f"Data folder {data_dir} needs inbox/ and attachments/ (set SDOC_DATA in .env)")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()

    ignore = shutil.ignore_patterns("node_modules", "dist", "__pycache__", "*.pyc", "ground_truth.json")
    shutil.copytree(ROOT / "sdoc", BUILD / "sdoc", ignore=ignore)
    shutil.copytree(ROOT / "frontend", BUILD / "frontend", ignore=ignore)
    for name in ("requirements.txt", "Dockerfile", ".dockerignore"):
        shutil.copy(ROOT / name, BUILD / name)
    (BUILD / "README.md").write_text(SPACE_README, encoding="utf-8")

    data_out = BUILD / "data"
    data_out.mkdir()
    shutil.copytree(data_dir / "inbox", data_out / "inbox")
    shutil.copytree(data_dir / "attachments", data_out / "attachments")
    if (data_dir / "sample_submission.json").is_file():
        shutil.copy(data_dir / "sample_submission.json", data_out / "sample_submission.json")

    leaked = list(BUILD.rglob("ground_truth*.json")) + list(BUILD.rglob(".env"))
    if leaked:
        sys.exit(f"REFUSING: secret/answer-key files ended up in the build: {leaked}")
    n_emails = len(list((data_out / "inbox").glob("*.json")))
    n_files = sum(1 for p in BUILD.rglob("*") if p.is_file())
    size_mb = sum(p.stat().st_size for p in BUILD.rglob("*") if p.is_file()) / 1e6
    print(f"Built {BUILD}: {n_files} files, {size_mb:.1f} MB, {n_emails} emails, no ground truth, no .env")


def upload():
    import os
    token, space = os.environ.get("HF_TOKEN"), os.environ.get("HF_SPACE")
    if not token or not space or "/" not in space:
        sys.exit("Set HF_TOKEN and HF_SPACE=user/space-name in .env")
    try:
        from huggingface_hub import HfApi
    except ImportError:
        sys.exit("pip install huggingface_hub")

    hf = HfApi(token=token)
    hf.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True, private=False)
    for key in SECRETS:
        value = os.environ.get(key)
        if value:
            hf.add_space_secret(space, key, value)
            print(f"secret   {key} set")
    for key in VARIABLES:
        value = os.environ.get(key)
        if value:
            hf.add_space_variable(space, key, value)
            print(f"variable {key}={value}")
    print("uploading ... (first time takes a minute)")
    hf.upload_folder(folder_path=str(BUILD), repo_id=space, repo_type="space",
                     commit_message="Deploy CargoSense")
    user, name = space.split("/", 1)
    print(f"\nDone. Build logs:  https://huggingface.co/spaces/{space}")
    print(f"Live site (after the build, ~5-10 min):  https://{user}-{name}.hf.space".lower().replace("_", "-"))


def main():
    ap = argparse.ArgumentParser(description="Deploy to a Hugging Face Space")
    ap.add_argument("--data", default=None, help="data folder (default: SDOC_DATA)")
    ap.add_argument("--dry-run", action="store_true", help="only build deploy_build/")
    args = ap.parse_args()
    data = args.data or config.data_source()
    if data.startswith("http"):
        sys.exit("Deploy needs a local data folder: pass --data path/to/data_v2")
    build(data)
    if not args.dry_run:
        upload()


if __name__ == "__main__":
    main()
