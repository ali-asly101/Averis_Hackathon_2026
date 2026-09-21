"""
One-command setup - works on Windows, macOS and Linux:

    python setup_project.py

1. creates a virtual environment in .venv (unless you're already in one)
2. creates .env from .env.example (if you don't have one yet)
3. installs the Python packages (requirements.txt) into the venv
4. installs + builds the web UI (if Node.js/npm is installed)
5. runs `python -m sdoc check` so you can see what's left to do
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def activate_hint():
    return (r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate")


def step(title):
    print(f"\n=== {title}")


def main():
    if sys.version_info < (3, 10):
        print(f"Python 3.10+ needed (you have {sys.version.split()[0]})")
        return 1

    # Not in a virtual environment? Make one and re-run this script inside it.
    # (Homebrew Python on macOS and most Linux distros refuse system-wide pip.)
    if sys.prefix == sys.base_prefix:
        step("Virtual environment")
        if not venv_python().exists():
            print(f"creating {VENV}")
            try:
                subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
            except subprocess.CalledProcessError:
                print("Could not create a virtual environment. On Debian/Ubuntu: "
                      "sudo apt install python3-venv, then re-run.")
                return 1
        else:
            print(f"using existing {VENV}")
        code = subprocess.call([str(venv_python()), str(Path(__file__).resolve())])
        print(f"\nFrom now on, activate it in each new terminal:  {activate_hint()}")
        return code

    step(".env")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print(".env already exists - leaving it alone")
    else:
        shutil.copy(example, env)
        print("created .env from .env.example -> open it and paste your LLM_API_KEY")

    step("Python packages")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    step("Web UI")
    npm = shutil.which("npm")
    if not npm:
        print("npm not found - skipping the UI. Install Node.js 20+ (nodejs.org) and re-run,")
        print("or use the pipeline from the terminal only (python -m sdoc run).")
    else:
        frontend = ROOT / "frontend"
        subprocess.check_call([npm, "install"], cwd=frontend)
        subprocess.check_call([npm, "run", "build"], cwd=frontend)

    step("Check")
    return subprocess.call([sys.executable, "-m", "sdoc", "check"], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
