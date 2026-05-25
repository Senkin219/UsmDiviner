from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class UsmChunk:
    index: int
    offset: int
    signature: int
    data_size: int
    data_offset: int
    padding_size: int
    chno: int
    data_type: int
    frame_time: int
    frame_rate: int
    payload_start: int
    payload_size: int

    @property
    def payload_end(self) -> int:
        return self.payload_start + self.payload_size


@dataclasses.dataclass
class HcaInfo:
    header_masked: bool
    version: int
    data_offset: int
    channel_count: int
    sample_rate: int
    block_count: int
    block_size: int
    ciph_type: int
    valid_crc_blocks: int = 0
    checked_crc_blocks: int = 0

    @property
    def ciph_label(self) -> str:
        if self.ciph_type == 0:
            return "none"
        if self.ciph_type == 1:
            return "keyless"
        if self.ciph_type == 0x38:
            return "keyed"
        return f"unknown:{self.ciph_type}"


@dataclasses.dataclass
class AudioDecision:
    channel: int
    format: str
    use_audio_mask: bool
    confidence: str
    reason: str
    hca: dict | None


@dataclasses.dataclass
class ProcessOptions:
    output_dir: str
    input_root: str | None
    vgmstream: str | None
    keep_intermediate_audio: bool
    adx_audio_mask: bool
    mux_mkv: bool
    ffmpeg: str | None
    write_report: bool
    fast: bool
    manual_key: int | None
    extract_only: bool
    audio_language_preset: str
