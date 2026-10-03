"""Stdlib-only ffmpeg video editing CLI for MindAlert."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT_ENV = "MINDALERT_VIDEO_ROOT"
DEFAULT_TIMEOUT = 900
DEFAULT_MAX_OUTPUT_SECONDS = 1800.0


class CliError(Exception):
    def __init__(self, code: str, detail: str, exit_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise CliError("invalid_argument", "invalid command arguments", 2)


def emit_json(document: Any, stream) -> None:
    json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def positive_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def positive_number(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def number(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def common_options(command: argparse.ArgumentParser, *, output: bool = False) -> None:
    command.add_argument("--timeout", type=positive_integer, default=DEFAULT_TIMEOUT)
    command.add_argument(
        "--max-output-seconds",
        type=positive_number,
        default=DEFAULT_MAX_OUTPUT_SECONDS,
    )
    if output:
        command.add_argument("--overwrite", action="store_true")


def make_parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="mindalert-video")
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check ffmpeg and ffprobe")
    common_options(doctor)

    probe = commands.add_parser("probe", help="inspect a media file")
    probe.add_argument("--input", required=True)
    common_options(probe)

    trim = commands.add_parser("trim", help="trim and re-encode a video")
    trim.add_argument("--input", required=True)
    trim.add_argument("--output", required=True)
    trim.add_argument("--start", required=True, type=number)
    trim.add_argument("--end", required=True, type=number)
    common_options(trim, output=True)

    concat = commands.add_parser("concat", help="concatenate and re-encode videos")
    concat.add_argument("--inputs", required=True, nargs="+")
    concat.add_argument("--output", required=True)
    common_options(concat, output=True)

    resize = commands.add_parser("resize", help="resize and re-encode a video")
    resize.add_argument("--input", required=True)
    resize.add_argument("--output", required=True)
    resize.add_argument("--width", required=True, type=positive_integer)
    resize.add_argument("--height", type=positive_integer)
    common_options(resize, output=True)

    extract = commands.add_parser("extract-audio", help="extract an audio stream")
    extract.add_argument("--input", required=True)
    extract.add_argument("--output", required=True)
    common_options(extract, output=True)

    thumbnail = commands.add_parser("thumbnail", help="extract one video frame")
    thumbnail.add_argument("--input", required=True)
    thumbnail.add_argument("--output", required=True)
    thumbnail.add_argument("--at", required=True, type=number)
    common_options(thumbnail, output=True)

    gif = commands.add_parser("gif", help="create a palette-optimized GIF")
    gif.add_argument("--input", required=True)
    gif.add_argument("--output", required=True)
    gif.add_argument("--start", required=True, type=number)
    gif.add_argument("--duration", required=True, type=positive_number)
    gif.add_argument("--width", required=True, type=positive_integer)
    gif.add_argument("--fps", type=positive_integer, default=10)
    common_options(gif, output=True)

    subtitles = commands.add_parser("burn-subtitles", help="burn SRT subtitles")
    subtitles.add_argument("--input", required=True)
    subtitles.add_argument("--srt", required=True)
    subtitles.add_argument("--output", required=True)
    common_options(subtitles, output=True)
    return root


def workspace_root() -> Path:
    raw = os.environ.get(ROOT_ENV)
    if not raw or not os.path.isabs(raw):
        raise CliError("root_missing", f"{ROOT_ENV} must be an absolute directory", 2)
    try:
        root = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CliError("root_missing", f"{ROOT_ENV} is not an accessible directory", 2) from error
    if not root.is_dir():
        raise CliError("root_missing", f"{ROOT_ENV} is not a directory", 2)
    return root


def validate_path(root: Path, raw: str, *, input_path: bool) -> Path:
    if not raw or "\x00" in raw:
        raise CliError("invalid_argument", "path must not be empty", 2)
    if "://" in raw:
        raise CliError("unsupported_input", "URL and protocol inputs are not supported", 2)
    if raw.startswith("-"):
        raise CliError("invalid_argument", "path must not start with '-'", 2)
    supplied = Path(raw)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved = candidate.resolve(strict=False)
        inside = os.path.commonpath((str(root), str(resolved))) == str(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CliError("path_escape", "path is outside MINDALERT_VIDEO_ROOT", 2) from error
    if not inside:
        raise CliError("path_escape", "path is outside MINDALERT_VIDEO_ROOT", 2)
    if input_path:
        if not resolved.exists():
            raise CliError("input_not_found", "input file was not found", 1)
        if not resolved.is_file():
            raise CliError("input_invalid", "input is not a regular file", 1)
    else:
        if not resolved.parent.is_dir():
            raise CliError("invalid_argument", "output directory does not exist", 2)
    return resolved


def executable(name: str) -> str | None:
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def dependencies(*, require_ffmpeg: bool) -> tuple[str | None, str]:
    ffmpeg = executable("ffmpeg") if require_ffmpeg else None
    ffprobe = executable("ffprobe")
    missing = []
    if require_ffmpeg and ffmpeg is None:
        missing.append("ffmpeg")
    if ffprobe is None:
        missing.append("ffprobe")
    if missing:
        raise CliError("dependency_missing", f"missing dependencies: {', '.join(missing)}", 1)
    return ffmpeg, ffprobe or ""


def run_process(argv: list[str], timeout: int, *, failure: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CliError("timeout", "media process exceeded --timeout", 1) from error
    except OSError as error:
        raise CliError("dependency_missing", "media dependency could not be executed", 1) from error
    if result.returncode != 0:
        raise CliError(failure, "media processing failed", 1)
    return result


def parse_fraction(raw: Any) -> float | None:
    if not isinstance(raw, str) or raw in {"", "0/0", "N/A"}:
        return None
    try:
        numerator, denominator = raw.split("/", 1)
        value = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) else None


def probe_document(path: Path, timeout: int) -> dict[str, Any]:
    _, ffprobe = dependencies(require_ffmpeg=False)
    result = run_process(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        timeout,
        failure="input_invalid",
    )
    try:
        document = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("input_invalid", "ffprobe returned invalid media information", 1) from error
    if not isinstance(document, dict) or not isinstance(document.get("streams"), list):
        raise CliError("input_invalid", "input is not valid media", 1)
    return document


def float_value(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def duration_of(info: dict[str, Any]) -> float:
    duration = float_value(info.get("format", {}).get("duration"))
    if duration is None:
        durations = [float_value(stream.get("duration")) for stream in info["streams"]]
        valid = [value for value in durations if value is not None]
        duration = max(valid) if valid else None
    if duration is None:
        raise CliError("input_invalid", "media duration is unavailable", 1)
    return duration


def video_stream(info: dict[str, Any]) -> dict[str, Any]:
    for stream in info["streams"]:
        if stream.get("codec_type") == "video":
            return stream
    raise CliError("input_invalid", "input has no video stream", 1)


def has_audio(info: dict[str, Any]) -> bool:
    return any(stream.get("codec_type") == "audio" for stream in info["streams"])


def public_probe(path: Path, info: dict[str, Any]) -> dict[str, Any]:
    streams = []
    for source in info["streams"]:
        stream = {
            "type": source.get("codec_type"),
            "codec": source.get("codec_name"),
            "width": source.get("width"),
            "height": source.get("height"),
            "fps": parse_fraction(source.get("avg_frame_rate")),
            "sample_rate": source.get("sample_rate"),
            "channels": source.get("channels"),
        }
        streams.append(stream)
    format_info = info.get("format", {})
    return {
        "duration_seconds": duration_of(info),
        "format": format_info.get("format_name"),
        "size_bytes": path.stat().st_size,
        "streams": streams,
    }


def enforce_limit(requested: float, maximum: float) -> None:
    if requested > maximum:
        raise CliError(
            "limit_exceeded",
            f"requested output duration exceeds --max-output-seconds {maximum:g}",
            2,
        )


def valid_position(position: float, duration: float, name: str) -> None:
    if position < 0:
        raise CliError("invalid_argument", f"{name} must be non-negative", 2)
    if position >= duration:
        raise CliError("seek_out_of_range", f"{name} is outside the input duration", 1)


def validate_output(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise CliError("output_exists", "output already exists; use --overwrite", 2)


def temporary_output(output: Path) -> Path:
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{output.stem}-",
        suffix=output.suffix,
        dir=output.parent,
    )
    os.close(descriptor)
    return Path(raw)


def output_result(root: Path, media_path: Path, output: Path, timeout: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "output": output.relative_to(root).as_posix(),
        "size_bytes": media_path.stat().st_size,
    }
    info = probe_document(media_path, timeout)
    duration = float_value(info.get("format", {}).get("duration"))
    if duration is not None:
        result["duration_seconds"] = duration
    videos = [stream for stream in info["streams"] if stream.get("codec_type") == "video"]
    if videos:
        result["width"] = videos[0].get("width")
        result["height"] = videos[0].get("height")
    return result


def produce(
    root: Path,
    output: Path,
    overwrite: bool,
    timeout: int,
    build_arguments: Callable[[Path], list[str]],
) -> dict[str, Any]:
    validate_output(output, overwrite)
    ffmpeg, _ = dependencies(require_ffmpeg=True)
    temporary = temporary_output(output)
    try:
        argv = [ffmpeg or "", "-nostdin", "-v", "error", "-y", *build_arguments(temporary)]
        run_process(argv, timeout, failure="ffmpeg_failed")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise CliError("ffmpeg_failed", "ffmpeg did not create a valid output", 1)
        result = output_result(root, temporary, output, timeout)
        if output.exists() and not overwrite:
            raise CliError("output_exists", "output already exists; use --overwrite", 2)
        os.replace(temporary, output)
        return result
    except OSError as error:
        raise CliError("ffmpeg_failed", "output could not be placed", 1) from error
    finally:
        temporary.unlink(missing_ok=True)


def extension(path: Path, allowed: set[str], label: str) -> str:
    suffix = path.suffix.lower()
    if suffix not in allowed:
        choices = ", ".join(sorted(allowed))
        raise CliError("unsupported_input", f"{label} output extension must be one of: {choices}", 2)
    return suffix


def doctor_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    ffmpeg = executable("ffmpeg")
    ffprobe = executable("ffprobe")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if path is None]
    if missing:
        raise CliError("dependency_missing", f"missing dependencies: {', '.join(missing)}", 1)
    ffmpeg_version = run_process([ffmpeg or "", "-version"], arguments.timeout, failure="dependency_missing")
    ffprobe_version = run_process([ffprobe or "", "-version"], arguments.timeout, failure="dependency_missing")
    encoders = run_process([ffmpeg or "", "-hide_banner", "-encoders"], arguments.timeout, failure="dependency_missing")
    filters = run_process([ffmpeg or "", "-hide_banner", "-filters"], arguments.timeout, failure="dependency_missing")
    try:
        ffmpeg_line = ffmpeg_version.stdout.decode("utf-8", "replace").splitlines()[0]
        ffprobe_line = ffprobe_version.stdout.decode("utf-8", "replace").splitlines()[0]
    except IndexError as error:
        raise CliError("dependency_missing", "media dependency version is unavailable", 1) from error
    writable = os.access(root, os.W_OK | os.X_OK)
    return {
        "ok": True,
        "ffmpeg": {
            "version": ffmpeg_line,
            "libx264": b"libx264" in encoders.stdout,
            "subtitles_filter": b"subtitles" in filters.stdout,
        },
        "ffprobe": {"version": ffprobe_line},
        "root": {"path": str(root), "writable": writable},
    }


def trim_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    if arguments.start < 0 or arguments.end <= arguments.start:
        raise CliError("invalid_argument", "trim requires 0 <= start < end", 2)
    info = probe_document(source, arguments.timeout)
    duration = duration_of(info)
    if arguments.start >= duration or arguments.end > duration:
        raise CliError("seek_out_of_range", "trim range is outside the input duration", 1)
    requested = arguments.end - arguments.start
    enforce_limit(requested, arguments.max_output_seconds)
    return produce(
        root,
        output,
        arguments.overwrite,
        arguments.timeout,
        lambda target: [
            "-i", str(source), "-ss", str(arguments.start), "-t", str(requested),
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(target),
        ],
    )


def concat_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    if len(arguments.inputs) < 2:
        raise CliError("invalid_argument", "concat requires at least two inputs", 2)
    sources = [validate_path(root, raw, input_path=True) for raw in arguments.inputs]
    output = validate_path(root, arguments.output, input_path=False)
    infos = [probe_document(source, arguments.timeout) for source in sources]
    durations = [duration_of(info) for info in infos]
    enforce_limit(sum(durations), arguments.max_output_seconds)
    videos = [video_stream(info) for info in infos]
    first_video = videos[0]
    width = first_video.get("width")
    height = first_video.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise CliError("input_invalid", "first input dimensions are unavailable", 1)
    include_audio = all(has_audio(info) for info in infos)
    filters = []
    for index in range(len(sources)):
        filters.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
        if include_audio:
            filters.append(
                f"[{index}:a:0]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a{index}]"
            )
    if include_audio:
        joined = "".join(f"[v{index}][a{index}]" for index in range(len(sources)))
        filters.append(f"{joined}concat=n={len(sources)}:v=1:a=1[outv][outa]")
    else:
        joined = "".join(f"[v{index}]" for index in range(len(sources)))
        filters.append(f"{joined}concat=n={len(sources)}:v=1:a=0[outv]")

    def build(target: Path) -> list[str]:
        result: list[str] = []
        for source in sources:
            result += ["-i", str(source)]
        result += ["-filter_complex", ";".join(filters), "-map", "[outv]"]
        if include_audio:
            result += ["-map", "[outa]", "-c:a", "aac"]
        result += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)]
        return result

    return produce(root, output, arguments.overwrite, arguments.timeout, build)


def resize_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    info = probe_document(source, arguments.timeout)
    video_stream(info)
    enforce_limit(duration_of(info), arguments.max_output_seconds)
    scale = f"scale={arguments.width}:{arguments.height}" if arguments.height else f"scale={arguments.width}:-2"
    return produce(
        root,
        output,
        arguments.overwrite,
        arguments.timeout,
        lambda target: [
            "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-vf", scale,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(target),
        ],
    )


def extract_audio_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    suffix = extension(output, {".wav", ".mp3", ".m4a"}, "audio")
    info = probe_document(source, arguments.timeout)
    if not has_audio(info):
        raise CliError("no_audio_stream", "input has no audio stream", 1)
    enforce_limit(duration_of(info), arguments.max_output_seconds)
    codecs = {".wav": "pcm_s16le", ".mp3": "libmp3lame", ".m4a": "aac"}
    return produce(
        root,
        output,
        arguments.overwrite,
        arguments.timeout,
        lambda target: ["-i", str(source), "-map", "0:a:0", "-vn", "-c:a", codecs[suffix], str(target)],
    )


def thumbnail_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    extension(output, {".jpg", ".png"}, "thumbnail")
    info = probe_document(source, arguments.timeout)
    video_stream(info)
    valid_position(arguments.at, duration_of(info), "--at")
    return produce(
        root,
        output,
        arguments.overwrite,
        arguments.timeout,
        lambda target: [
            "-ss", str(arguments.at), "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
            "-update", "1", str(target),
        ],
    )


def gif_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    extension(output, {".gif"}, "GIF")
    info = probe_document(source, arguments.timeout)
    video_stream(info)
    source_duration = duration_of(info)
    valid_position(arguments.start, source_duration, "--start")
    if arguments.start + arguments.duration > source_duration:
        raise CliError("seek_out_of_range", "GIF range is outside the input duration", 1)
    enforce_limit(arguments.duration, arguments.max_output_seconds)
    graph = (
        f"fps={arguments.fps},scale={arguments.width}:-2:flags=lanczos,split[gif_a][gif_b];"
        "[gif_a]palettegen[palette];[gif_b][palette]paletteuse"
    )
    return produce(
        root,
        output,
        arguments.overwrite,
        arguments.timeout,
        lambda target: [
            "-ss", str(arguments.start), "-t", str(arguments.duration), "-i", str(source),
            "-filter_complex", graph, "-an", str(target),
        ],
    )


def escape_ffmpeg(value: str, special: str) -> str:
    return "".join(f"\\{character}" if character in special else character for character in value)


def filter_path(path: Path) -> str:
    option_value = escape_ffmpeg(str(path), "\\':")
    return escape_ffmpeg(option_value, "\\'[],;")


def burn_subtitles_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    source = validate_path(root, arguments.input, input_path=True)
    subtitle = validate_path(root, arguments.srt, input_path=True)
    output = validate_path(root, arguments.output, input_path=False)
    info = probe_document(source, arguments.timeout)
    video_stream(info)
    enforce_limit(duration_of(info), arguments.max_output_seconds)
    descriptor, raw_safe = tempfile.mkstemp(prefix=".mindalert-subtitles-", suffix=".srt", dir=root)
    safe_subtitle = Path(raw_safe)
    try:
        with os.fdopen(descriptor, "wb") as destination, subtitle.open("rb") as original:
            shutil.copyfileobj(original, destination)
        subtitle_filter = f"subtitles=filename={filter_path(safe_subtitle)}"
        return produce(
            root,
            output,
            arguments.overwrite,
            arguments.timeout,
            lambda target: [
                "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-vf", subtitle_filter,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(target),
            ],
        )
    except OSError as error:
        raise CliError("input_invalid", "subtitle file could not be prepared", 1) from error
    finally:
        safe_subtitle.unlink(missing_ok=True)


def dispatch(arguments: argparse.Namespace, root: Path) -> dict[str, Any]:
    handlers = {
        "doctor": doctor_command,
        "trim": trim_command,
        "concat": concat_command,
        "resize": resize_command,
        "extract-audio": extract_audio_command,
        "thumbnail": thumbnail_command,
        "gif": gif_command,
        "burn-subtitles": burn_subtitles_command,
    }
    if arguments.command == "probe":
        source = validate_path(root, arguments.input, input_path=True)
        return public_probe(source, probe_document(source, arguments.timeout))
    return handlers[arguments.command](root, arguments)


def main(argv: list[str]) -> int:
    try:
        arguments = make_parser().parse_args(argv)
        root = workspace_root()
        emit_json(dispatch(arguments, root), sys.stdout)
        return 0
    except CliError as error:
        emit_json({"error": error.code, "detail": error.detail}, sys.stderr)
        return error.exit_code
    except (OSError, ValueError, KeyError, TypeError):
        emit_json({"error": "input_invalid", "detail": "media input could not be processed"}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
