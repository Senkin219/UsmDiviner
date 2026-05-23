from __future__ import annotations

import json
import logging
import mmap
from collections import defaultdict
from pathlib import Path

from .audio import decide_audio_for_channel
from .constants import (
    AUDIO_PROBE_BYTES_PER_CHANNEL,
    SOLVER_BEAM,
    SOLVER_L1_BEAM,
    FAST_CRACK_VIDEO_BYTES,
    SIG_SFA,
    SIG_SFV,
)
from .crack import crack_keys_from_usm
from .exceptions import ExternalToolError, KeyCrackError
from .formats import classify_audio, detect_video_stream
from .keys import full_key_int, genshin_like_key, split_full_key
from .masks import make_masks, unmask_audio_payload, unmask_video_payload
from .models import AudioDecision, ProcessOptions, UsmChunk
from .tools import (
    decode_with_vgmstream,
    find_ffmpeg,
    find_vgmstream,
    mux_to_mkv,
    remove_hcakey_files,
    write_hcakey_file,
)
from .usm import parse_usm_chunks

logger = logging.getLogger(__name__)

AUDIO_LANGUAGE_METADATA = {
    0: ("chi", "中文"),
    1: ("eng", "English"),
    2: ("jpn", "日本語"),
    3: ("kor", "한국어"),
}


def process_one(usm_path_str: str, opt: ProcessOptions) -> dict:
    usm_path = Path(usm_path_str)
    base = usm_path.stem
    out_dir = _make_output_dir(usm_path, opt)
    out_dir.mkdir(parents=True, exist_ok=True)

    if usm_path.stat().st_size == 0:
        raise KeyCrackError({"reason": "empty USM file"})

    if opt.extract_only:
        with usm_path.open("rb") as fp, mmap.mmap(fp.fileno(), 0, access=mmap.ACCESS_READ) as data:
            chunks = parse_usm_chunks(data)
            video_path, video_info, audio_paths, audio_info = _demux_raw_streams(
                data,
                chunks,
                out_dir,
                base,
            )
        report = _build_extract_report(
            usm_path,
            out_dir,
            chunks,
            video_path,
            video_info,
            audio_paths,
            audio_info,
        )
        if opt.write_report:
            (out_dir / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report["report_written"] = True
        else:
            report["report_written"] = False
        return report

    if opt.manual_key is not None:
        key1, key2 = split_full_key(opt.manual_key)
        crack_stats = {"skipped": True, "reason": "manual key supplied"}
    else:
        try:
            key1, key2, crack_stats = _crack_key(usm_path, opt.fast)
        except KeyCrackError as exc:
            return _skip_report(usm_path, out_dir, exc.args[0], opt.write_report)

    video_mask1, video_mask2, audio_mask = make_masks(key1, key2)

    with usm_path.open("rb") as fp, mmap.mmap(fp.fileno(), 0, access=mmap.ACCESS_READ) as data:
        chunks = parse_usm_chunks(data)
        audio_payloads = _collect_audio_probe_payloads(data, chunks)

        vgmstream = find_vgmstream(opt.vgmstream)
        audio_decisions: dict[int, AudioDecision] = {}
        for ch, payloads in audio_payloads.items():
            decision, _ = decide_audio_for_channel(
                ch,
                payloads,
                audio_mask,
                key1,
                key2,
                vgmstream,
                opt.adx_audio_mask,
            )
            audio_decisions[ch] = decision

        video_path, video_info, audio_paths = _demux_streams(
            data,
            chunks,
            out_dir,
            base,
            video_mask1,
            video_mask2,
            audio_mask,
            audio_decisions,
        )

    decoded = _decode_audio(audio_paths, audio_decisions, vgmstream, key1, key2)
    mux_report, mux_success = _maybe_mux(
        opt,
        video_path,
        audio_paths,
        audio_decisions,
        decoded,
        out_dir,
        base,
    )
    _cleanup_outputs(
        mux_success,
        opt.keep_intermediate_audio,
        out_dir,
        video_path,
        audio_paths,
        decoded,
    )
    _clear_removed_paths(mux_success, opt.keep_intermediate_audio, decoded)

    report = _build_report(
        usm_path,
        out_dir,
        key1,
        key2,
        crack_stats,
        chunks,
        video_path,
        video_info,
        audio_paths,
        audio_decisions,
        decoded,
        mux_report,
    )
    if opt.manual_key is not None:
        report["manual_key"] = True
    if opt.write_report:
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["report_written"] = True
    else:
        report["report_written"] = False
    return report


def _make_output_dir(usm_path: Path, opt: ProcessOptions) -> Path:
    output_root = Path(opt.output_dir)
    if not opt.input_root:
        return output_root / usm_path.stem

    try:
        rel = usm_path.resolve().relative_to(Path(opt.input_root).resolve())
    except ValueError:
        rel = Path(usm_path.name)
    return output_root / rel.with_suffix("")


def _crack_key(usm_path: Path, fast: bool) -> tuple[bytes, bytes, dict]:
    key1, key2, crack_stats = crack_keys_from_usm(
        usm_path,
        max_video_bytes=FAST_CRACK_VIDEO_BYTES if fast else None,
        beam_size=SOLVER_BEAM,
        l1_beam_size=SOLVER_L1_BEAM,
    )
    if key1 is None or key2 is None:
        raise KeyCrackError(crack_stats)
    return key1, key2, crack_stats


def _skip_report(usm_path: Path, out_dir: Path, crack_stats: dict, write_report: bool) -> dict:
    report = {
        "file": str(usm_path),
        "status": "skipped",
        "output_dir": str(out_dir),
        "reason": crack_stats.get("reason", "key recovery failed"),
        "crack": crack_stats,
    }
    if write_report:
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["report_written"] = True
    else:
        report["report_written"] = False
    return report


def _collect_audio_probe_payloads(data, chunks: list[UsmChunk]) -> dict[int, list[bytes]]:
    payloads: dict[int, list[bytes]] = defaultdict(list)
    remaining: dict[int, int] = defaultdict(lambda: AUDIO_PROBE_BYTES_PER_CHANNEL)

    for chunk in chunks:
        if chunk.signature != SIG_SFA or chunk.data_type != 0 or chunk.payload_size <= 0:
            continue
        left = remaining[chunk.chno]
        if left <= 0:
            continue
        take = min(chunk.payload_size, left)
        payloads[chunk.chno].append(data[chunk.payload_start:chunk.payload_start + take])
        remaining[chunk.chno] -= take
    return payloads


def _demux_streams(
    data,
    chunks: list[UsmChunk],
    out_dir: Path,
    base: str,
    video_mask1: bytes,
    video_mask2: bytes,
    audio_mask: bytes,
    audio_decisions: dict[int, AudioDecision],
) -> tuple[Path | None, dict, dict[int, Path]]:
    temp_video_path = out_dir / f"{base}.video.tmp"
    audio_fps: dict[int, object] = {}
    audio_paths: dict[int, Path] = {}

    with temp_video_path.open("wb") as video_fp:
        try:
            for chunk in chunks:
                payload = data[chunk.payload_start:chunk.payload_end]
                if chunk.signature == SIG_SFV and chunk.data_type == 0:
                    video_fp.write(unmask_video_payload(payload, video_mask1, video_mask2))
                elif chunk.signature == SIG_SFA and chunk.data_type == 0:
                    decision = audio_decisions.get(chunk.chno)
                    if not decision:
                        continue
                    out_payload = (
                        unmask_audio_payload(payload, audio_mask)
                        if decision.use_audio_mask
                        else payload
                    )
                    ext = decision.format if decision.format in ("hca", "adx") else "bin"
                    if chunk.chno not in audio_fps:
                        audio_path = out_dir / f"{base}_ch{chunk.chno}.{ext}"
                        audio_paths[chunk.chno] = audio_path
                        audio_fps[chunk.chno] = audio_path.open("wb")
                    audio_fps[chunk.chno].write(out_payload)
        finally:
            for fp in audio_fps.values():
                fp.close()

    video_path, video_info = _finalize_video_temp(temp_video_path, out_dir, base)
    return video_path, video_info, audio_paths


def _demux_raw_streams(
    data,
    chunks: list[UsmChunk],
    out_dir: Path,
    base: str,
) -> tuple[Path | None, dict, dict[int, Path], dict[int, dict]]:
    temp_video_path = out_dir / f"{base}.video.tmp"
    audio_fps: dict[int, object] = {}
    temp_audio_paths: dict[int, Path] = {}

    with temp_video_path.open("wb") as video_fp:
        try:
            for chunk in chunks:
                payload = data[chunk.payload_start:chunk.payload_end]
                if chunk.signature == SIG_SFV and chunk.data_type == 0:
                    video_fp.write(payload)
                elif chunk.signature == SIG_SFA and chunk.data_type == 0:
                    if chunk.chno not in audio_fps:
                        audio_path = out_dir / f"{base}_ch{chunk.chno}.audio.tmp"
                        temp_audio_paths[chunk.chno] = audio_path
                        audio_fps[chunk.chno] = audio_path.open("wb")
                    audio_fps[chunk.chno].write(payload)
        finally:
            for fp in audio_fps.values():
                fp.close()

    video_path, video_info = _finalize_video_temp(temp_video_path, out_dir, base)
    audio_paths: dict[int, Path] = {}
    audio_info: dict[int, dict] = {}
    for ch, temp_path in sorted(temp_audio_paths.items()):
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            _safe_unlink(temp_path)
            continue
        with temp_path.open("rb") as fp:
            cls = classify_audio(fp.read(4096))
        fmt = cls.get("format") or "unknown"
        ext = fmt if fmt in ("hca", "adx") else "bin"
        audio_path = out_dir / f"{base}_ch{ch}.{ext}"
        if audio_path.exists():
            audio_path.unlink()
        temp_path.rename(audio_path)
        audio_paths[ch] = audio_path
        audio_info[ch] = {"format": fmt, "path": str(audio_path), "raw": True}
    return video_path, video_info, audio_paths, audio_info


def _finalize_video_temp(
    temp_video_path: Path,
    out_dir: Path,
    base: str,
) -> tuple[Path | None, dict]:
    if not temp_video_path.exists() or temp_video_path.stat().st_size == 0:
        _safe_unlink(temp_video_path)
        return None, {"format": "none", "extension": None, "codec": None, "magic": ""}

    with temp_video_path.open("rb") as fp:
        video_info = detect_video_stream(fp.read(4096))
    ext = video_info.get("extension") or "bin"
    video_path = out_dir / f"{base}.{ext}"
    if video_path.exists():
        video_path.unlink()
    temp_video_path.rename(video_path)
    return video_path, video_info


def _decode_audio(
    audio_paths: dict[int, Path],
    audio_decisions: dict[int, AudioDecision],
    vgmstream: str | None,
    key1: bytes,
    key2: bytes,
) -> dict[int, dict]:
    decoded: dict[int, dict] = {}
    for ch, audio_path in audio_paths.items():
        decision = audio_decisions[ch]
        if decision.format == "hca":
            write_hcakey_file(audio_path, key1, key2)
        if not vgmstream:
            continue
        wav_path = audio_path.with_suffix(".wav")
        try:
            ok, log = decode_with_vgmstream(vgmstream, audio_path, wav_path)
        except ExternalToolError as exc:
            logger.warning("audio decode failed for %s: %s", audio_path.name, exc)
            decoded[ch] = {"ok": False, "wav": None, "log_tail": str(exc)}
            continue
        decoded[ch] = {"ok": ok, "wav": str(wav_path) if ok else None, "log_tail": log[-1000:]}
    return decoded


def _maybe_mux(
    opt: ProcessOptions,
    video_path: Path | None,
    audio_paths: dict[int, Path],
    audio_decisions: dict[int, AudioDecision],
    decoded: dict[int, dict],
    out_dir: Path,
    base: str,
) -> tuple[dict | None, bool]:
    if not opt.mux_mkv:
        return None, False
    if not video_path:
        return {
            "ok": False,
            "mkv": None,
            "log_tail": "video stream not found; MKV not created",
        }, False

    mux_audio_inputs: list[Path] = []
    mux_audio_metadata: list[tuple[str, str] | None] = []
    for ch, audio_path in sorted(audio_paths.items()):
        dec = decoded.get(ch) or {}
        if dec.get("ok") and dec.get("wav"):
            mux_audio_inputs.append(Path(dec["wav"]))
            mux_audio_metadata.append(AUDIO_LANGUAGE_METADATA.get(ch))
        elif audio_decisions[ch].format == "adx":
            mux_audio_inputs.append(audio_path)
            mux_audio_metadata.append(AUDIO_LANGUAGE_METADATA.get(ch))

    ffmpeg = find_ffmpeg(opt.ffmpeg)
    if not ffmpeg:
        return {"ok": False, "mkv": None, "log_tail": "ffmpeg not found"}, False

    mkv_path = out_dir / f"{base}.mkv"
    try:
        ok, log = mux_to_mkv(
            ffmpeg,
            video_path,
            mux_audio_inputs,
            mkv_path,
            audio_metadata=mux_audio_metadata,
        )
    except ExternalToolError as exc:
        logger.warning("mkv mux failed for %s: %s", base, exc)
        message = "ffmpeg mux failed; extracted streams were kept"
        return {
            "ok": False,
            "mkv": None,
            "message": message,
            "log_tail": str(exc),
            "streams_kept": True,
        }, False

    if not ok:
        message = _mux_failure_message(video_path, log)
        logger.warning("mkv mux skipped for %s: %s", base, message)
        return {
            "ok": False,
            "mkv": None,
            "message": message,
            "log_tail": log[-1000:],
            "streams_kept": True,
        }, False

    return {"ok": True, "mkv": str(mkv_path), "log_tail": log[-1000:]}, True


def _mux_failure_message(video_path: Path, log: str) -> str:
    text = log.lower()
    if "unknown timestamp" in text:
        return (
            "ffmpeg could not mux the video stream because timestamps are missing; "
            "extracted streams were kept"
        )
    return "ffmpeg mux failed; extracted streams were kept"


def _cleanup_outputs(
    mux_success: bool,
    keep_intermediate_audio: bool,
    out_dir: Path,
    video_path: Path | None,
    audio_paths: dict[int, Path],
    decoded: dict[int, dict],
) -> None:
    if mux_success:
        stream_paths = set(audio_paths.values())
        stream_paths.update(
            Path(dec["wav"])
            for dec in decoded.values()
            if dec.get("ok") and dec.get("wav")
        )
        if video_path:
            stream_paths.add(video_path)
        for path in stream_paths:
            _safe_unlink(path)
        remove_hcakey_files(out_dir)
        return

    if not keep_intermediate_audio:
        for ch, audio_path in audio_paths.items():
            if bool((decoded.get(ch) or {}).get("ok")):
                _safe_unlink(audio_path)
                _safe_unlink(audio_path.with_suffix(audio_path.suffix + "key"))


def _clear_removed_paths(
    mux_success: bool,
    keep_intermediate_audio: bool,
    decoded: dict[int, dict],
) -> None:
    for dec in decoded.values():
        wav = dec.get("wav")
        if wav and not Path(wav).exists():
            dec["wav"] = None
            if mux_success:
                dec["removed_after_mux"] = True
            elif not keep_intermediate_audio:
                dec["removed_after_decode"] = True


def _build_report(
    usm_path: Path,
    out_dir: Path,
    key1: bytes,
    key2: bytes,
    crack_stats: dict,
    chunks: list[UsmChunk],
    video_path: Path | None,
    video_info: dict,
    audio_paths: dict[int, Path],
    audio_decisions: dict[int, AudioDecision],
    decoded: dict[int, dict],
    mux_report: dict | None,
) -> dict:
    full_key = full_key_int(key1, key2)
    return {
        "file": str(usm_path),
        "status": "ok",
        "output_dir": str(out_dir),
        "key1_hex_little": key1.hex().upper(),
        "key2_hex_little": key2.hex().upper(),
        "full_key_hex": f"{full_key:016X}",
        "full_key_decimal": str(full_key),
        "genshin_like_key": genshin_like_key(full_key, usm_path.name),
        "crack": crack_stats,
        "chunks": {
            "total": len(chunks),
            "video": sum(1 for c in chunks if c.signature == SIG_SFV and c.data_type == 0),
            "audio": sum(1 for c in chunks if c.signature == SIG_SFA and c.data_type == 0),
        },
        "video": {
            "path": str(video_path) if video_path and video_path.exists() else None,
            "format": video_info.get("format"),
            "codec": video_info.get("codec"),
            "magic": video_info.get("magic"),
        },
        "audio": {
            str(ch): {
                "path": (
                    str(audio_paths[ch])
                    if ch in audio_paths and audio_paths[ch].exists()
                    else None
                ),
                "format": decision.format,
                "use_audio_mask": decision.use_audio_mask,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "hca": decision.hca,
                "decode": decoded.get(ch),
            }
            for ch, decision in sorted(audio_decisions.items())
        },
        "mux": mux_report,
    }


def _build_extract_report(
    usm_path: Path,
    out_dir: Path,
    chunks: list[UsmChunk],
    video_path: Path | None,
    video_info: dict,
    audio_paths: dict[int, Path],
    audio_info: dict[int, dict],
) -> dict:
    return {
        "file": str(usm_path),
        "status": "ok",
        "output_dir": str(out_dir),
        "extract_only": True,
        "chunks": {
            "total": len(chunks),
            "video": sum(1 for c in chunks if c.signature == SIG_SFV and c.data_type == 0),
            "audio": sum(1 for c in chunks if c.signature == SIG_SFA and c.data_type == 0),
        },
        "video": {
            "path": str(video_path) if video_path and video_path.exists() else None,
            "format": video_info.get("format"),
            "codec": video_info.get("codec"),
            "magic": video_info.get("magic"),
            "raw": True,
        },
        "audio": {
            str(ch): {
                "path": str(path) if path.exists() else None,
                "format": audio_info.get(ch, {}).get("format", "unknown"),
                "raw": True,
            }
            for ch, path in sorted(audio_paths.items())
        },
        "mux": None,
    }


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.debug("failed to remove %s: %s", path, exc)
