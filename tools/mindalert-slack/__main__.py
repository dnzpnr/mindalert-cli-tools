"""Minimal stdlib-only Slack Web API CLI for MindAlert."""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import socket
import ssl
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_API_BASE = "https://slack.com/api"
TOKEN_ENV = "SLACK_BOT_TOKEN"
API_BASE_ENV = "SLACK_API_BASE"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class CliError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        exit_code: int,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        self.retry_after = retry_after


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise CliError("invalid_arguments", "invalid command arguments", 2)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def emit_json(document: Any, stream) -> None:
    json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def network_error_category(error: BaseException) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_verification_failed"
    if isinstance(reason, socket.gaierror):
        return "dns_failure"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if getattr(reason, "errno", None) in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return "network_unreachable"
    return "network_error_other"


def redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            redact(key, secret) if isinstance(key, str) else key: redact(item, secret)
            for key, item in value.items()
        }
    return value


def safe_provider_code(value: Any, token: str) -> str:
    if not isinstance(value, str) or not value or token in value:
        return "api_error"
    return value


def validate_api_base(raw_base: str) -> str:
    try:
        parsed = urlsplit(raw_base)
        port = parsed.port
    except ValueError as error:
        raise CliError("insecure_api_base", f"invalid {API_BASE_ENV}", 2) from error

    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CliError("insecure_api_base", f"invalid {API_BASE_ENV}", 2)
    if port is not None and not 1 <= port <= 65535:
        raise CliError("insecure_api_base", f"invalid {API_BASE_ENV}", 2)
    if parsed.scheme == "https":
        return raw_base.rstrip("/")
    if parsed.scheme != "http":
        raise CliError("insecure_api_base", f"{API_BASE_ENV} must use HTTPS", 2)

    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return raw_base.rstrip("/")
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise CliError(
            "insecure_api_base",
            f"HTTP {API_BASE_ENV} must use a loopback address",
            2,
        )
    return raw_base.rstrip("/")


def parse_json_response(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_response", "Slack returned invalid JSON", 1) from error
    if not isinstance(document, dict):
        raise CliError("invalid_response", "Slack returned an invalid response", 1)
    return document


def read_response_body(response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise CliError(
                    "response_too_large",
                    "Slack response exceeded the 8 MiB limit",
                    3,
                )
        except ValueError:
            pass
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(
            "response_too_large",
            "Slack response exceeded the 8 MiB limit",
            3,
        )
    return raw


def retry_after(headers) -> int:
    try:
        value = int(headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def api_call(
    base: str,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base}/{method}"
    if params:
        url = f"{url}?{urlencode(params)}"
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "mindalert-slack/0.1.0",
    }
    request_method = "GET"
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        request_method = "POST"

    request = Request(url, data=body, headers=headers, method=request_method)
    try:
        with build_opener(NoRedirects).open(
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            document = parse_json_response(read_response_body(response))
    except HTTPError as error:
        raw = read_response_body(error)
        if error.code == 429:
            raise CliError(
                "rate_limited",
                "Slack rate limit exceeded",
                5,
                retry_after=retry_after(error.headers),
            ) from error
        try:
            document = parse_json_response(raw)
        except CliError as parse_error:
            raise CliError("api_error", "Slack API request failed", 1) from parse_error
        code = safe_provider_code(document.get("error"), token)
        raise CliError(code, "Slack API request failed", 1) from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        category = network_error_category(error)
        raise CliError(
            "network_error", f"Slack API request failed ({category})", 3
        ) from error

    if document.get("ok") is not True:
        code = safe_provider_code(document.get("error"), token)
        raise CliError(code, "Slack API rejected the request", 1)
    return document


def parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="mindalert-slack")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("auth-test", help="verify Slack authentication")

    history = commands.add_parser("history", help="read channel or thread messages")
    history.add_argument("--channel", required=True)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--cursor")
    history.add_argument("--oldest")
    history.add_argument("--thread")

    commands.add_parser("send", help="send stdin JSON to Slack")
    return root


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    token_option = any(
        argument == "--token" or argument.startswith("--token=") for argument in argv
    )
    if token_option:
        raise CliError("invalid_arguments", "token options are not supported", 2)
    arguments = parser().parse_args(argv)
    if arguments.command == "history":
        if arguments.limit < 1:
            raise CliError("invalid_arguments", "--limit must be positive", 2)
        if not arguments.channel.strip():
            raise CliError("invalid_arguments", "--channel must not be empty", 2)
    return arguments


def read_send_payload() -> dict[str, str]:
    try:
        payload = json.load(sys.stdin)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError(
            "invalid_input", "stdin must contain one JSON object", 2
        ) from error
    if not isinstance(payload, dict):
        raise CliError("invalid_input", "stdin must contain one JSON object", 2)
    if set(payload) - {"channel", "text", "thread_ts"}:
        raise CliError("invalid_input", "stdin contains unsupported fields", 2)
    channel = payload.get("channel")
    text = payload.get("text")
    thread = payload.get("thread_ts")
    if not isinstance(channel, str) or not channel.strip():
        raise CliError("invalid_input", "channel must be a non-empty string", 2)
    if not isinstance(text, str) or not text:
        raise CliError("invalid_input", "text must be a non-empty string", 2)
    if thread is not None and (not isinstance(thread, str) or not thread.strip()):
        raise CliError("invalid_input", "thread_ts must be a non-empty string", 2)
    result = {"channel": channel, "text": text}
    if thread is not None:
        result["thread_ts"] = thread
    return result


def history_result(document: dict[str, Any]) -> dict[str, Any]:
    messages = document.get("messages")
    if not isinstance(messages, list):
        raise CliError("invalid_response", "Slack response has no messages list", 1)
    metadata = document.get("response_metadata")
    cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
    cursor = cursor if isinstance(cursor, str) and cursor else None
    truncated = bool(document.get("has_more") or cursor)
    if truncated and cursor is None:
        raise CliError("invalid_response", "Slack omitted the next cursor", 1)
    result: dict[str, Any] = {
        "ok": True,
        "messages": messages,
        "truncated": truncated,
    }
    if cursor is not None:
        result["next_cursor"] = cursor
    return result


def run(argv: list[str]) -> dict[str, Any]:
    arguments = parse_arguments(argv)
    send_payload = read_send_payload() if arguments.command == "send" else None

    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise CliError("missing_credentials", f"set {TOKEN_ENV}", 4)
    base = validate_api_base(os.environ.get(API_BASE_ENV, DEFAULT_API_BASE))

    if arguments.command == "auth-test":
        return redact(api_call(base, token, "auth.test"), token)
    if arguments.command == "send":
        assert send_payload is not None
        document = api_call(base, token, "chat.postMessage", payload=send_payload)
        result: dict[str, Any] = {
            "ok": True,
            "channel": document.get("channel"),
            "ts": document.get("ts"),
        }
        message = document.get("message")
        thread = message.get("thread_ts") if isinstance(message, dict) else None
        if thread is None:
            thread = send_payload.get("thread_ts")
        if thread is not None:
            result["thread_ts"] = thread
        return redact(result, token)

    params: dict[str, Any] = {
        "channel": arguments.channel,
        "limit": arguments.limit,
    }
    if arguments.cursor:
        params["cursor"] = arguments.cursor
    if arguments.oldest:
        params["oldest"] = arguments.oldest
    method = "conversations.history"
    if arguments.thread:
        method = "conversations.replies"
        params["ts"] = arguments.thread
    return redact(history_result(api_call(base, token, method, params=params)), token)


def main() -> int:
    try:
        result = run(sys.argv[1:])
    except CliError as error:
        payload: dict[str, Any] = {"error": error.code, "detail": error.detail}
        if error.retry_after is not None:
            payload["retry_after"] = error.retry_after
        emit_json(payload, sys.stderr)
        return error.exit_code
    emit_json(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
