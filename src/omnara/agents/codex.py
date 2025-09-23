import contextlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from omnara.sdk.client import OmnaraClient


def _platform_tag() -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        ext = ""
        tag = f"darwin-{arch}"
    elif system == "Linux":
        arch = "x64" if machine in ("x86_64", "amd64") else machine
        ext = ""
        tag = f"linux-{arch}"
    elif system == "Windows":
        arch = "x64" if machine in ("amd64", "x86_64") else machine
        ext = ".exe"
        tag = f"win-{arch}"
    else:
        # Fallback for unknown
        arch = machine or "unknown"
        ext = ""
        tag = f"{system.lower()}-{arch}"
    return tag, ext, system


def _packaged_binary_path() -> Path:
    """Return packaged binary path inside the wheel, if present."""
    tag, ext, _ = _platform_tag()
    base = Path(__file__).resolve().parent.parent / "_bin" / "codex" / tag
    return base / f"codex{ext}"


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _omnara_libs_dir() -> Path:
    return _package_root().parent / "omnara.libs"


def _extract_from_wheel(dest: Path, libs_dir: Path) -> bool:
    """Download the platform wheel and extract the Codex binary/libs."""
    try:
        from importlib.metadata import version as pkg_version
    except Exception:  # pragma: no cover - best effort fallback
        return False

    try:
        current_version = pkg_version("omnara")
    except Exception:
        return False

    tag, ext, _ = _platform_tag()
    target_entry = f"omnara/_bin/codex/{tag}/codex{ext}"

    with tempfile.TemporaryDirectory(prefix="omnara-fetch-") as tmp_dir:
        wheel_dir = Path(tmp_dir)
        try:
            print(
                "[omnara] Codex binary not bundled; attempting to fetch packaged wheel...",
                file=sys.stderr,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    f"omnara=={current_version}",
                    "--only-binary=:all:",
                    "--no-deps",
                    "--dest",
                    str(wheel_dir),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception:
            return False

        wheel_path = next(wheel_dir.glob("omnara-*.whl"), None)
        if not wheel_path:
            return False

        try:
            with zipfile.ZipFile(wheel_path) as zf:
                if target_entry not in zf.namelist():
                    print(
                        "[omnara] Downloaded wheel missing Codex binary; aborting fetch.",
                        file=sys.stderr,
                    )
                    return False

                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(target_entry) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                members = [n for n in zf.namelist() if n.startswith("omnara.libs/")]
                if members:
                    libs_dir.mkdir(parents=True, exist_ok=True)
                    for name in members:
                        rel = name.split("/", 1)[1]
                        if not rel:
                            continue
                        target = libs_dir / rel
                        if name.endswith("/"):
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)

        except Exception:
            return False

        if os.name != "nt":
            with contextlib.suppress(Exception):
                mode = os.stat(dest).st_mode
                os.chmod(dest, mode | 0o111)

        print(
            "[omnara] Codex binary fetched from wheel and cached locally.",
            file=sys.stderr,
        )
        return dest.exists()


def _env_binary_path() -> Optional[Path]:
    """Return a path from OMNARA_CODEX_PATH if set.

    Accepts either a direct file path to the binary or a directory, in which case
    we append the platform-specific binary name (codex[.exe]).
    """
    p = os.environ.get("OMNARA_CODEX_PATH")
    if not p:
        return None
    p = os.path.expanduser(p)
    path = Path(p)
    if path.is_dir():
        tag, ext, _ = _platform_tag()
        return path / f"codex{ext}"
    return path


def _resolve_codex_binary() -> Path:
    # 1) explicit override via env var
    env_p = _env_binary_path()
    if env_p and env_p.exists():
        return env_p

    # 2) packaged in the wheel
    packaged = _packaged_binary_path()
    if packaged.exists():
        return packaged

    libs_dir = _omnara_libs_dir()
    if _extract_from_wheel(packaged, libs_dir):
        return packaged

    raise FileNotFoundError(
        "Codex binary not found.\n"
        "Set OMNARA_CODEX_PATH to specify the binary path.\n"
        f"Otherwise, expected a packaged binary in the wheel at: {_packaged_binary_path()}\n\n"
        "To build in local omnara repo:\n"
        "  cd integrations/cli_wrappers/codex/codex-rs && cargo build --release -p codex-cli\n"
        "The built binary will be at:\n"
        "  integrations/cli_wrappers/codex/codex-rs/target/release/codex\n"
        "Then set OMNARA_CODEX_PATH to either the binary file or its directory."
    )


def run_codex(args, unknown_args, api_key: str):
    """Launch the Codex CLI binary and keep the agent session alive via heartbeat.

    Mirrors the Claude wrapper behavior by sending periodic heartbeats to the
    dashboard while the Codex subprocess is running.
    """
    try:
        bin_path = _resolve_codex_binary()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    env = os.environ.copy()

    # Wire Omnara env for the Rust client
    env["OMNARA_API_KEY"] = api_key
    if getattr(args, "base_url", None):
        env["OMNARA_API_URL"] = args.base_url
    # Ensure there is a stable session ID shared with the Rust process
    session_id = env.setdefault("OMNARA_SESSION_ID", str(uuid.uuid4()))

    libs_dir = _omnara_libs_dir()
    if libs_dir.exists():
        if os.name == "nt":
            sep = ";"
            env_var = "PATH"
        else:
            sep = ":"
            env_var = "LD_LIBRARY_PATH"
        current = env.get(env_var)
        if current:
            if str(libs_dir) not in current.split(sep):
                env[env_var] = f"{libs_dir}{sep}{current}"
        else:
            env[env_var] = str(libs_dir)

    # Ensure executable bit if running from packaged file on Unix
    try:
        if bin_path.is_file() and os.name != "nt":
            mode = os.stat(bin_path).st_mode
            # 0o111 owner/group/other execute bits
            if (mode & 0o111) == 0:
                os.chmod(bin_path, mode | 0o111)
    except Exception:
        pass

    cmd = [str(bin_path)]
    if unknown_args:
        cmd.extend(unknown_args)

    # Start a background heartbeat loop similar to the Claude wrapper.
    # This may 404 until the Codex process creates the instance; that's fine.
    stop_event = threading.Event()

    def _heartbeat_loop(
        api_key: str,
        base_url: Optional[str],
        agent_instance_id: str,
        interval: float = 30.0,
    ) -> None:
        try:
            client = OmnaraClient(
                api_key=api_key,
                base_url=(base_url or "https://agent-dashboard-mcp.onrender.com"),
            )
            session = client.session
            url = (base_url or "https://agent-dashboard-mcp.onrender.com").rstrip(
                "/"
            ) + f"/api/v1/agents/instances/{agent_instance_id}/heartbeat"

            import random

            time.sleep(random.uniform(0, 2.0))
            while not stop_event.is_set():
                try:
                    resp = session.post(url, timeout=10)
                    _ = resp.status_code  # ignore; 404 expected until instance exists
                except Exception:
                    pass

                # Sleep with jitter; ensure a minimum reasonable delay
                delay = interval + random.uniform(-2.0, 2.0)
                if delay < 5:
                    delay = 5
                end_time = time.time() + delay
                while time.time() < end_time and not stop_event.is_set():
                    time.sleep(0.1)
        except Exception:
            # Never let heartbeat failures crash the launcher
            pass

    base_url = getattr(args, "base_url", None) or env.get("OMNARA_API_URL")
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(api_key, base_url, session_id),
        daemon=True,
    )
    hb_thread.start()

    try:
        subprocess.run(cmd, env=env, check=False)
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        # Signal heartbeat thread to exit and join briefly
        stop_event.set()
        try:
            hb_thread.join(timeout=2.0)
        except Exception:
            pass
