from __future__ import annotations

import argparse
import concurrent.futures as futures
import logging
import os
from pathlib import Path

from .exceptions import UsmDivinerError
from .keys import parse_full_key
from .models import ProcessOptions
from .processor import process_one
from .tools import find_ffmpeg, find_vgmstream
from .usm import collect_usm_inputs

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UsmDiviner: recover USM keys, decrypt streams, and demux CRI USM files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="USM file or directory containing .usm files")
    parser.add_argument("-o", "--output", default="output", help="output directory")
    parser.add_argument("--no-parallel", action="store_true", help="disable multiprocessing")
    parser.add_argument("--report", action="store_true", help="write per-file report.json")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="use only the first 50 MB of encrypted video data for key recovery",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="full 64-bit USM key as hexadecimal; skips key recovery",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="do not decrypt or recover key; only extract raw streams",
    )
    parser.add_argument(
        "--vgmstream",
        default=None,
        help="path to vgmstream-cli; auto-detected if omitted",
    )
    parser.add_argument(
        "--keep-intermediate-audio",
        action="store_true",
        help="keep extracted .hca/.adx after successful WAV decode when not muxing",
    )
    parser.add_argument(
        "--no-adx-audiomask",
        action="store_true",
        help="do not apply AudioMask to ADX streams by default",
    )
    parser.add_argument(
        "--mux-mkv",
        action="store_true",
        help="mux decrypted video and usable audio into MKV",
    )
    parser.add_argument(
        "--audio-language-preset",
        choices=("none", "gi"),
        metavar="<gamename>",
        default="none",
        help=(
            "audio language metadata preset for MKV muxing; "
            "requires --mux-mkv and maps tracks by game convention"
        ),
    )
    parser.add_argument(
        "--ffmpeg",
        default=None,
        help="path to ffmpeg for --mux-mkv; auto-detected if omitted",
    )
    args = parser.parse_args(argv)
    if args.key is not None:
        try:
            args.key = parse_full_key(args.key)
        except ValueError as exc:
            parser.error(f"--key: {exc}")
    if args.key is not None and args.fast:
        parser.error("--key cannot be used with --fast")
    if args.extract_only and args.key is not None:
        parser.error("--extract-only cannot be used with --key")
    if args.extract_only and args.fast:
        parser.error("--extract-only cannot be used with --fast")
    if args.extract_only and args.mux_mkv:
        parser.error("--extract-only cannot be used with --mux-mkv")
    if not args.mux_mkv and args.audio_language_preset != "none":
        parser.error("--audio-language-preset requires --mux-mkv")
    return args


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("input not found: %s", input_path)
        return 2

    files = collect_usm_inputs(input_path)
    if not files:
        logger.error("no .usm files found")
        return 2

    opt = ProcessOptions(
        output_dir=args.output,
        input_root=str(input_path) if input_path.is_dir() else None,
        vgmstream=args.vgmstream,
        keep_intermediate_audio=args.keep_intermediate_audio,
        adx_audio_mask=not args.no_adx_audiomask,
        mux_mkv=args.mux_mkv,
        ffmpeg=args.ffmpeg,
        write_report=args.report,
        fast=args.fast,
        manual_key=args.key,
        extract_only=args.extract_only,
        audio_language_preset=args.audio_language_preset,
    )

    Path(args.output).mkdir(parents=True, exist_ok=True)
    max_workers = max(os.cpu_count() or 1, 1)
    use_parallel = (not args.no_parallel) and max_workers > 1 and len(files) > 1

    logger.info(
        "USM files=%s | multiprocessing=%s | workers=%s | fast=%s | extract_only=%s",
        len(files),
        use_parallel,
        max_workers if use_parallel else 1,
        args.fast,
        args.extract_only,
    )
    if args.key is not None:
        logger.info("manual key: %016X", args.key)
    if not args.extract_only:
        logger.info(
            "vgmstream: %s",
            find_vgmstream(args.vgmstream) or "not found; audio will be extracted only",
        )
    if args.mux_mkv:
        logger.info("ffmpeg: %s", find_ffmpeg(args.ffmpeg) or "not found; MKV mux will be skipped")

    reports: list[dict] = []
    if use_parallel:
        with futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(process_one, str(path), opt): path for path in files}
            for fut in futures.as_completed(future_map):
                report = _future_report(fut, future_map[fut])
                reports.append(report)
                print_summary(report)
    else:
        for path in files:
            try:
                report = process_one(str(path), opt)
            except UsmDivinerError as exc:
                report = {"file": str(path), "status": "error", "reason": str(exc)}
                logger.error("%s: %s", path.name, exc)
            except Exception as exc:  # noqa: BLE001 - keep CLI resilient for batch jobs.
                report = {"file": str(path), "status": "error", "reason": repr(exc)}
                logger.exception("%s: unexpected error", path.name)
            reports.append(report)
            print_summary(report)

    ok = sum(1 for r in reports if r.get("status") == "ok")
    skipped = sum(1 for r in reports if r.get("status") == "skipped")
    errors = sum(1 for r in reports if r.get("status") == "error")
    logger.info("Done. ok=%s, skipped=%s, errors=%s", ok, skipped, errors)
    return 0 if errors == 0 else 1


def _future_report(fut: futures.Future, path: Path) -> dict:
    try:
        return fut.result()
    except UsmDivinerError as exc:
        logger.error("%s: %s", path.name, exc)
        return {"file": str(path), "status": "error", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - keep CLI resilient for batch jobs.
        logger.exception("%s: unexpected error", path.name)
        return {"file": str(path), "status": "error", "reason": repr(exc)}


def print_summary(report: dict) -> None:
    file = Path(report.get("file", "?")).name
    if report.get("status") != "ok":
        logger.warning("[SKIP] %s: %s", file, report.get("reason"))
        return

    logger.info("[OK] %s", file)
    if report.get("extract_only"):
        logger.info("     mode: extract-only")
    else:
        logger.info(
            "     key: %s  key1=%s key2=%s",
            report["full_key_hex"],
            report["key1_hex_little"],
            report["key2_hex_little"],
        )
    video = report.get("video") or {}
    if video.get("path"):
        logger.info("     video: %s (%s)", video["path"], video.get("format") or "unknown")

    for ch, audio in report.get("audio", {}).items():
        if audio.get("raw"):
            logger.info("     audio ch%s: %s (raw)", ch, audio.get("format") or "unknown")
            continue
        hca = audio.get("hca") or {}
        hca_str = ""
        if audio["format"] == "hca":
            ciph_type = hca.get("ciph_type")
            ciph_map = {0: "none", 1: "keyless", 56: "keyed"}
            hca_str = f", hca_ciph={ciph_type}({ciph_map.get(ciph_type, 'unknown')})"
        dec = audio.get("decode") or {}
        wav = f", wav={dec.get('wav')}" if dec.get("ok") else ""
        logger.info(
            "     audio ch%s: %s, audiomask=%s (%s)%s%s",
            ch,
            audio["format"],
            audio["use_audio_mask"],
            audio["confidence"],
            hca_str,
            wav,
        )

    mux = report.get("mux")
    if mux:
        if mux.get("ok"):
            logger.info("     mkv: %s", mux.get("mkv"))
        else:
            logger.info("     mkv: skipped (%s)", mux.get("message") or mux.get("log_tail"))
    if report.get("report_written"):
        logger.info("     report: %s", Path(report["output_dir"]) / "report.json")
