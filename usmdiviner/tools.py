from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .exceptions import ExternalToolError
from .keys import full_key_int

logger = logging.getLogger(__name__)


def _existing_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    return str(p) if p.is_file() else None


def find_vgmstream(user_path: str | None) -> str | None:
    if user_path:
        return _existing_file(user_path)

    names = ("vgmstream-cli.exe", "vgmstream-cli", "test.exe", "test", "vgmstream")
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit

    roots = [Path.cwd(), Path(__file__).resolve().parent.parent]
    common_dirs = (
        Path("vgmstream-win64"),
        Path("vgmstream"),
        Path("bin"),
        Path("tools") / "vgmstream",
    )
    for root in roots:
        for rel in common_dirs:
            for name in names:
                hit = _existing_file(root / rel / name)
                if hit:
                    return hit

    if os.name != "nt":
        for path in (
            "/usr/local/bin/vgmstream-cli",
            "/opt/homebrew/bin/vgmstream-cli",
            "/usr/bin/vgmstream-cli",
        ):
            hit = _existing_file(path)
            if hit:
                return hit
    return None


def find_ffmpeg(user_path: str | None) -> str | None:
    if user_path:
        return _existing_file(user_path)
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def write_hcakey_file(audio_path: Path, key1: bytes, key2: bytes) -> Path:
    key_bytes = full_key_int(key1, key2).to_bytes(8, "big")
    target = audio_path.with_suffix(audio_path.suffix + "key")
    target.write_bytes(key_bytes)
    return target


def remove_hcakey_files(directory: Path) -> None:
    for pattern in ("*.hcakey", ".hcakey"):
        for key_path in directory.glob(pattern):
            try:
                key_path.unlink()
            except OSError as exc:
                logger.debug("failed to remove %s: %s", key_path, exc)


def decode_with_vgmstream(
    vgmstream: str,
    input_path: Path,
    output_wav: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [vgmstream, "-o", str(output_wav), str(input_path)]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExternalToolError(f"vgmstream failed: {exc}") from exc
    ok = proc.returncode == 0 and output_wav.exists() and output_wav.stat().st_size > 44
    return ok, proc.stdout[-4000:]


def mux_to_mkv(
    ffmpeg: str,
    video_path: Path,
    audio_inputs: list[Path],
    output_mkv: Path,
    timeout: int = 300,
    *,
    audio_languages: list[str | None] | None = None,
) -> tuple[bool, str]:
    if not video_path.exists() or video_path.stat().st_size == 0:
        return False, "video stream does not exist"

    existing_audio: list[tuple[Path, str | None]] = []
    for i, path in enumerate(audio_inputs):
        if not path.exists() or path.stat().st_size == 0:
            continue
        language = audio_languages[i] if audio_languages and i < len(audio_languages) else None
        existing_audio.append((path, language))

    output_mkv.parent.mkdir(parents=True, exist_ok=True)
    _safe_unlink(output_mkv)

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path)]
    for ap, _ in existing_audio:
        cmd.extend(["-i", str(ap)])
    cmd.extend(["-map", "0:v:0"])
    for i in range(len(existing_audio)):
        cmd.extend(["-map", f"{i + 1}:a:0"])
    cmd.extend(["-c:v", "copy"])
    if existing_audio:
        cmd.extend(["-c:a", "flac"])
    if audio_languages and len(existing_audio) == len(audio_languages):
        for i, (_, language) in enumerate(existing_audio):
            if language:
                cmd.extend([
                    f"-metadata:s:a:{i}",
                    f"language={language}",
                ])
    cmd.append(str(output_mkv))

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _safe_unlink(output_mkv)
        raise ExternalToolError(f"ffmpeg failed: {exc}") from exc

    ok = proc.returncode == 0 and output_mkv.exists() and output_mkv.stat().st_size > 0
    if not ok:
        _safe_unlink(output_mkv)
    return ok, proc.stdout[-4000:]


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.debug("failed to remove %s: %s", path, exc)
