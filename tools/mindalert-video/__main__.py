"""Stdlib-only ffmpeg video editing CLI for MindAlert."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT_ENV = "MINDALERT_VIDEO_ROOT"
DEFAULT_TIMEOUT = 900
DEFAULT_MAX_OUTPUT_SECONDS = 1800.0
SCANNED_SUFFIXES = {".html", ".css", ".js", ".svg"}
SKIPPED_PROJECT_DIRS = {".git", "node_modules"}
CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
SOURCE_ATTRIBUTE_RE = re.compile(
    r"(?i)(?:\b(?:src|href|data)\s*=\s*)(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s>]+))"
)
CSS_URL_RE = re.compile(
    r"(?i)\burl\(\s*(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s)'\"]+))\s*\)"
)
CSS_IMPORT_RE = re.compile(
    r"(?i)@import\s+(?:url\(\s*)?(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s;)]+))"
)
JS_IMPORT_RE = re.compile(
    r"(?i)(?:\bimport\s*\(|\b(?:import|export)\b[^;]*?\bfrom\s*)"
    r"[\"'](?P<quoted>[^\"']*)[\"']"
)
NETWORK_CALL_RE = re.compile(
    r"(?i)(?:\bfetch\s*\(|\bXMLHttpRequest\s*\(|\bWebSocket\s*\(|"
    r"\bEventSource\s*\(|\.sendBeacon\s*\()"
)


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

    check = commands.add_parser("check", help="scan a HyperFrames project")
    check.add_argument("--project", required=True)

    render = commands.add_parser("render", help="render a HyperFrames project")
    render.add_argument("--project", required=True)
    render.add_argument("--output", required=True)
    common_options(render, output=True)
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


def validate_project(root: Path, raw: str) -> Path:
    if not raw or "\x00" in raw or raw.startswith("-"):
        raise CliError("invalid_argument", "project path is invalid", 2)
    supplied = Path(raw)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved = candidate.resolve(strict=False)
        inside = os.path.commonpath((str(root), str(resolved))) == str(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CliError("path_escape", "path is outside MINDALERT_VIDEO_ROOT", 2) from error
    if not inside:
        raise CliError("path_escape", "path is outside MINDALERT_VIDEO_ROOT", 2)
    if not resolved.exists():
        raise CliError("input_not_found", "project directory was not found", 1)
    if not resolved.is_dir():
        raise CliError("input_invalid", "project is not a directory", 1)
    return resolved


def executable(name: str) -> str | None:
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def configured_executable(variable: str, fallback: str) -> str | None:
    configured = os.environ.get(variable)
    if configured is not None:
        candidate = Path(configured)
        if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            return None
        return str(candidate.resolve())
    found = shutil.which(fallback, path=os.environ.get("PATH", ""))
    return str(Path(found).resolve()) if found else None


def browser_executable() -> str | None:
    configured = os.environ.get("HYPERFRAMES_BROWSER_PATH")
    candidates = (configured,) if configured is not None else CHROME_CANDIDATES
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def sandbox_tools() -> tuple[str | None, str | None, str]:
    original_path = os.environ.get("PATH", "")
    unshare = shutil.which("unshare", path=original_path)
    search_parts = [part for part in original_path.split(os.pathsep) if part]
    for extra in ("/usr/sbin", "/sbin"):
        if extra not in search_parts:
            search_parts.append(extra)
    child_path = os.pathsep.join(search_parts)
    ip = shutil.which("ip", path=child_path)
    return (
        str(Path(unshare).resolve()) if unshare else None,
        str(Path(ip).resolve()) if ip else None,
        child_path,
    )


def hyperframes_owner_home(hyperframes: str) -> Path | None:
    path = Path(hyperframes)
    if path.parent.name == "bin" and path.parent.parent.name == ".local":
        return path.parent.parent.parent
    return None


def hyperframes_child_path(base_path: str, hyperframes: str) -> str:
    parts = [part for part in base_path.split(os.pathsep) if part]
    if shutil.which("node", path=base_path) is not None:
        return base_path
    owner_home = hyperframes_owner_home(hyperframes)
    candidates = [Path("/usr/local/bin/node"), Path("/usr/bin/node"), Path("/bin/node")]
    if owner_home is not None:
        candidates.extend(sorted(owner_home.glob(".nvm/versions/node/*/bin/node"), reverse=True))
        candidates.append(owner_home / ".local" / "bin" / "node")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            directory = str(candidate.parent)
            if directory not in parts:
                parts.append(directory)
            break
    return os.pathsep.join(parts)


def prepare_hyperframes_home(home: Path, hyperframes: str) -> None:
    owner_home = hyperframes_owner_home(hyperframes)
    if owner_home is None:
        return
    installation = owner_home / ".local" / "share" / "hyperframes"
    if not installation.is_dir():
        return
    share = home / ".local" / "share"
    share.mkdir(parents=True)
    (share / "hyperframes").symlink_to(installation, target_is_directory=True)


def source_references(line: str) -> list[str]:
    references = []
    for pattern in (SOURCE_ATTRIBUTE_RE, CSS_URL_RE, CSS_IMPORT_RE, JS_IMPORT_RE):
        for match in pattern.finditer(line):
            value = match.groupdict().get("quoted") or match.groupdict().get("bare")
            if value is not None:
                references.append(value.strip())
    return references


def external_reference(value: str) -> bool:
    lowered = value.lstrip().lower()
    return lowered.startswith(("http://", "https://", "//"))


def reference_escapes(project: Path, source: Path, value: str) -> bool:
    if external_reference(value) or not value or value.startswith(("/", "#")):
        return False
    without_fragment = value.split("#", 1)[0].split("?", 1)[0]
    if not without_fragment or ":" in without_fragment.split("/", 1)[0]:
        return False
    try:
        candidate = (source.parent / without_fragment).resolve(strict=False)
        return os.path.commonpath((str(project), str(candidate))) != str(project)
    except (OSError, RuntimeError, ValueError):
        return True


def symlink_escapes(project: Path, path: Path) -> bool:
    try:
        target = path.resolve(strict=False)
        return os.path.commonpath((str(project), str(target))) != str(project)
    except (OSError, RuntimeError, ValueError):
        return True


def scan_project(project: Path) -> tuple[int, list[str], list[str]]:
    files = []
    path_findings: list[str] = []
    for current, directories, names in os.walk(project, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for name in sorted(directories):
            if name in SKIPPED_PROJECT_DIRS:
                continue
            path = current_path / name
            if path.is_symlink():
                if symlink_escapes(project, path):
                    path_findings.append(path.relative_to(project).as_posix())
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() and symlink_escapes(project, path):
                path_findings.append(path.relative_to(project).as_posix())
                continue
            if path.suffix.lower() in SCANNED_SUFFIXES:
                files.append(path)

    external_findings: list[str] = []
    for path in files:
        relative = path.relative_to(project).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            raise CliError("input_invalid", "project file could not be read", 1) from error
        for line_number, line in enumerate(lines, start=1):
            location = f"{relative}:{line_number}"
            references = source_references(line)
            if NETWORK_CALL_RE.search(line) or any(external_reference(value) for value in references):
                if location not in external_findings:
                    external_findings.append(location)
            if any(reference_escapes(project, path, value) for value in references):
                if location not in path_findings:
                    path_findings.append(location)
    return len(files), external_findings, path_findings


def checked_project(project: Path) -> int:
    count, external_findings, path_findings = scan_project(project)
    if path_findings:
        raise CliError("path_escape", ", ".join(path_findings), 2)
    if external_findings:
        raise CliError("external_reference_blocked", ", ".join(external_findings), 2)
    return count


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


def system_temp_parent(root: Path) -> Path:
    candidates = (tempfile.gettempdir(), "/tmp", "/var/tmp")
    for raw in candidates:
        try:
            candidate = Path(raw).resolve(strict=True)
            inside = os.path.commonpath((str(root), str(candidate))) == str(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not inside and candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    raise CliError("render_failed", "system temporary directory is unavailable", 1)


def render_failure_detail(stderr: bytes, stdout: bytes) -> str:
    raw = stderr if stderr.strip() else stdout
    detail = raw.decode("utf-8", "replace").strip()
    return (detail or "hyperframes render failed")[:300]


def run_render_process(
    argv: list[str], cwd: Path, environment: dict[str, str], timeout: int
) -> None:
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise CliError("render_failed", "hyperframes could not be executed", 1) from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise CliError("timeout", "render exceeded --timeout", 1) from error
    if process.returncode != 0:
        raise CliError("render_failed", render_failure_detail(stderr, stdout), 1)


def place_rendered_output(source: Path, output: Path, overwrite: bool) -> None:
    staged = temporary_output(output)
    try:
        with source.open("rb") as original, staged.open("wb") as destination:
            shutil.copyfileobj(original, destination)
            destination.flush()
            os.fsync(destination.fileno())
        if output.exists() and not overwrite:
            raise CliError("output_exists", "output already exists; use --overwrite", 2)
        os.replace(staged, output)
    except CliError:
        raise
    except OSError as error:
        raise CliError("render_failed", "output could not be placed", 1) from error
    finally:
        staged.unlink(missing_ok=True)


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
    hyperframes = configured_executable("HYPERFRAMES_BIN", "hyperframes")
    unshare, ip, _ = sandbox_tools()
    sandbox_available = unshare is not None and ip is not None
    chrome = browser_executable()
    return {
        "ok": True,
        "ffmpeg": {
            "version": ffmpeg_line,
            "libx264": b"libx264" in encoders.stdout,
            "subtitles_filter": b"subtitles" in filters.stdout,
        },
        "ffprobe": {"version": ffprobe_line},
        "root": {"path": str(root), "writable": writable},
        "hyperframes": {"found": hyperframes is not None, "path": hyperframes},
        "sandbox": {
            "available": sandbox_available,
            "method": "unshare" if sandbox_available else None,
        },
        "chrome": chrome,
        "render_ready": hyperframes is not None and sandbox_available and chrome is not None,
    }


def check_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    project = validate_project(root, arguments.project)
    files_scanned = checked_project(project)
    return {"ok": True, "files_scanned": files_scanned, "findings": []}


def render_command(root: Path, arguments: argparse.Namespace) -> dict[str, Any]:
    project = validate_project(root, arguments.project)
    checked_project(project)

    unshare, ip, child_path = sandbox_tools()
    if unshare is None or ip is None:
        raise CliError("sandbox_unavailable", "unshare network sandbox is unavailable", 2)
    hyperframes = configured_executable("HYPERFRAMES_BIN", "hyperframes")
    if hyperframes is None:
        raise CliError("dependency_missing", "hyperframes", 1)

    output = validate_path(root, arguments.output, input_path=False)
    extension(output, {".mp4"}, "render")
    validate_output(output, arguments.overwrite)
    chrome = browser_executable()
    temporary_parent = system_temp_parent(root)

    try:
        with tempfile.TemporaryDirectory(prefix="mindalert-video-", dir=temporary_parent) as raw_temp:
            temporary = Path(raw_temp)
            project_copy = temporary / "project"
            shutil.copytree(
                project,
                project_copy,
                ignore=shutil.ignore_patterns(*SKIPPED_PROJECT_DIRS),
                symlinks=True,
            )
            home = temporary / "home"
            child_tmp = temporary / "tmp"
            home.mkdir()
            child_tmp.mkdir()
            prepare_hyperframes_home(home, hyperframes)
            rendered = temporary / "out.mp4"
            child_environment = {
                "HOME": str(home),
                "PATH": hyperframes_child_path(child_path, hyperframes),
                "LANG": os.environ.get("LANG") or "C.UTF-8",
                "TMPDIR": str(child_tmp),
                "HYPERFRAMES_NO_TELEMETRY": "1",
                "DO_NOT_TRACK": "1",
                "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            }
            if chrome is not None:
                child_environment["HYPERFRAMES_BROWSER_PATH"] = chrome
            argv = [
                unshare,
                "--user",
                "--map-root-user",
                "--net",
                "sh",
                "-c",
                'ip link set lo up; exec "$@"',
                "sh",
                hyperframes,
                "render",
                "--output",
                str(rendered),
            ]
            run_render_process(argv, project_copy, child_environment, arguments.timeout)
            if not rendered.is_file() or rendered.stat().st_size == 0:
                raise CliError("render_failed", "hyperframes did not create an output", 1)
            try:
                info = probe_document(rendered, arguments.timeout)
                duration = duration_of(info)
                video = video_stream(info)
            except CliError as error:
                if error.code == "dependency_missing":
                    raise
                raise CliError("render_failed", "rendered output is not valid media", 1) from error
            enforce_limit(duration, arguments.max_output_seconds)
            size_bytes = rendered.stat().st_size
            place_rendered_output(rendered, output, arguments.overwrite)
    except CliError:
        raise
    except OSError as error:
        raise CliError("render_failed", "temporary render workspace failed", 1) from error

    return {
        "output": output.relative_to(root).as_posix(),
        "duration_seconds": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "size_bytes": size_bytes,
        "sandbox": "unshare",
        "network": "blocked",
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
        "check": check_command,
        "render": render_command,
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
