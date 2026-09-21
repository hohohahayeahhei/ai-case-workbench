"""Local-only media validation and optional, bounded speech transcription.

No model or video is downloaded here. The caller supplies uploaded bytes and a
local model directory/file configured by the operator. Source text is data.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_MEDIA_SECONDS = 30 * 60


class MediaError(ValueError):
    pass


def detect_media(data: bytes, filename: str = "") -> tuple[str, str]:
    """Recognize bytes, never trust an upload's extension or path."""
    if not isinstance(data, bytes) or not data:
        raise MediaError("素材为空；请上传原始视频、音频或图片文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError("单个素材不能超过 200 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if data.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if data.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return ".mp3", "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] in {b"M4A ", b"M4B "}:
            return ".m4a", "audio/mp4"
        return ".mp4", "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".webm", "video/webm"
    raise MediaError("不支持的素材格式；不会把网页、脚本或播放列表当成视频")


def local_capabilities() -> dict[str, Any]:
    whisper_bin = os.environ.get("DOUYIN_WHISPER_CPP_BIN", "") or shutil.which("whisper-cli") or ""
    cpp_model = os.environ.get("DOUYIN_WHISPER_CPP_MODEL", "")
    faster_model = os.environ.get("DOUYIN_FASTER_WHISPER_MODEL", "")
    try:
        faster_installed = importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        faster_installed = False
    cpp_ready = bool(whisper_bin and Path(whisper_bin).is_file() and cpp_model and Path(cpp_model).is_file())
    faster_ready = bool(faster_installed and faster_model and Path(faster_model).is_dir())
    # Do not expose local model paths to the HTTP client or model prompts.
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
            "whisper_cpp": cpp_ready, "faster_whisper": faster_ready,
            "transcription_available": cpp_ready or faster_ready,
            "automatic_model_download": False, "visual_review": "manual_timestamped_observation"}


def probe_media(path: Path, timeout: int = 20) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"duration_seconds": None, "probe_status": "ffprobe_unavailable"}
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                                 "-show_entries", "format=duration:stream=codec_type,width,height",
                                 "-of", "json", str(path)], capture_output=True, timeout=timeout, check=True)
        payload = json.loads(result.stdout[:100_000])
        duration = float(payload.get("format", {}).get("duration", 0)) or None
        if duration is not None and (not math.isfinite(duration) or duration > MAX_MEDIA_SECONDS):
            raise MediaError("素材超过 30 分钟处理上限或时长无效")
        return {"duration_seconds": duration, "probe_status": "verified", "streams": payload.get("streams", [])[:8]}
    except (subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, MediaError):
            raise
        # Do not surface raw subprocess output, which may contain paths or data.
        raise MediaError("本地媒体解析失败：文件可能损坏或格式不受支持") from None


def _seconds(value: Any) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    parts = str(value).replace(",", ".").split(":")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def _normalize_segments(segments: list[dict[str, Any]], engine: str) -> list[dict[str, Any]]:
    result = []
    for segment in segments[:10000]:
        text = str(segment.get("text", "")).strip()
        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
        if not text or not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= MAX_MEDIA_SECONDS):
            continue
        result.append({"kind": "machine_transcript", "role": "context", "text": text[:8000],
                       "original_text": text[:8000], "language": str(segment.get("language", "unknown")),
                       "start_seconds": round(start, 3), "end_seconds": round(end, 3),
                       "machine_generated": True, "uncertainty": "机器语音识别，尚未逐句人工核对",
                       "engine": engine})
    if not result:
        raise MediaError("未识别到带有效时间位置的语音；不生成虚构转写")
    return result


def transcribe_local(path: Path, mime_type: str, timeout: int = 180) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use an already installed local engine; never call a text-model gateway."""
    if mime_type.startswith("image/"):
        raise MediaError("图片素材没有语音；请核对图片并录入带画面位置的可见证据")
    capabilities = local_capabilities()
    if not capabilities["transcription_available"]:
        raise MediaError("本机未配置本地转写模型；可安装 whisper.cpp 或 faster-whisper 并指定本地模型，或导入人工核对的时间段证据")
    metadata = probe_media(path)
    if capabilities["whisper_cpp"]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise MediaError("whisper.cpp 转写需要本地 ffmpeg 生成 16 kHz 单声道音频")
        executable = os.environ.get("DOUYIN_WHISPER_CPP_BIN", "") or shutil.which("whisper-cli")
        with tempfile.TemporaryDirectory(prefix="transcribe-", dir=path.parent) as temporary:
            wav, output = Path(temporary) / "audio.wav", Path(temporary) / "transcript"
            try:
                subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(path),
                                "-t", str(MAX_MEDIA_SECONDS), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                               capture_output=True, timeout=60, check=True)
                subprocess.run([str(executable), "-m", os.environ["DOUYIN_WHISPER_CPP_MODEL"], "-f", str(wav),
                                "-l", "auto", "-oj", "-of", str(output)], capture_output=True, timeout=timeout, check=True)
                json_path = output.with_suffix(".json")
                if not json_path.is_file() or json_path.stat().st_size > 4_000_000:
                    raise MediaError("转写结果缺失或超出大小限制")
                raw = json.loads(json_path.read_text())
                language = raw.get("result", {}).get("language", "unknown")
                segments = []
                for segment in raw.get("transcription", []):
                    offsets, timestamps = segment.get("offsets", {}), segment.get("timestamps", {})
                    start = float(offsets["from"]) / 1000 if "from" in offsets else _seconds(timestamps.get("from", 0))
                    end = float(offsets["to"]) / 1000 if "to" in offsets else _seconds(timestamps.get("to", 0))
                    segments.append({"start": start, "end": end, "text": segment.get("text"), "language": language})
                return _normalize_segments(segments, "whisper.cpp"), {**metadata, "engine": "whisper.cpp"}
            except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
                if isinstance(exc, MediaError):
                    raise
                raise MediaError("本地 whisper.cpp 转写失败或超时；原始素材已保留，可修复后续跑") from None
    # Run the optional Python engine in a subprocess so its timeout is enforceable.
    # local_files_only prevents package defaults from downloading models.
    import sys
    script = """import json,sys
from faster_whisper import WhisperModel
m=WhisperModel(sys.argv[1],device='cpu',compute_type='int8',local_files_only=True)
segments,info=m.transcribe(sys.argv[2],vad_filter=True)
out=[]
for s in segments:
 out.append({'start':s.start,'end':s.end,'text':s.text,'language':info.language})
 if len(out)>=10000: break
print(json.dumps(out,ensure_ascii=False))
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, os.environ["DOUYIN_FASTER_WHISPER_MODEL"], str(path)],
                                capture_output=True, timeout=timeout, check=True)
        if len(result.stdout) > 4_000_000:
            raise MediaError("转写结果超出大小限制")
        return _normalize_segments(json.loads(result.stdout), "faster-whisper"), {**metadata, "engine": "faster-whisper"}
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("本地 faster-whisper 转写失败或超时；原始素材已保留，可修复后续跑") from None
