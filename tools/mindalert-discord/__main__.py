"""Minimal stdlib-only Discord REST API CLI for MindAlert."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_API_BASE = "https://discord.com/api/v10"
TOKEN_ENV = "DISCORD_BOT_TOKEN"
API_BASE_ENV = "DISCORD_API_BASE"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "DiscordBot (https://github.com/dnzpnr/mindalert-cli-tools, 0.1.0)"
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


class CliError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        exit_code: int,
        *,
        retry_after: int | float | None = None,
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


def read_response_body(response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise CliError(
                    "response_too_large",
                    "Discord response exceeded the 8 MiB limit",
                    3,
                )
        except ValueError:
            pass
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(
            "response_too_large",
            "Discord response exceeded the 8 MiB limit",
            3,
        )
    return raw


def parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError(
            "invalid_response", "Discord returned invalid JSON", 1
        ) from error


def parse_object(raw: bytes) -> dict[str, Any]:
    document = parse_json(raw)
    if not isinstance(document, dict):
        raise CliError("invalid_response", "Discord returned an invalid response", 1)
    return document


def provider_error_code(document: dict[str, Any], status: int) -> str:
    if status == 401:
        return "unauthorized"
    code = document.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return f"discord_{code}"
    return "api_error"


def header_retry_after(headers) -> int | float:
    try:
        value = float(headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def rate_limit_error(error: HTTPError) -> CliError:
    raw = read_response_body(error)
    retry_after: int | float = header_retry_after(error.headers)
    try:
        document = parse_json(raw)
    except CliError:
        document = None
    if isinstance(document, dict):
        body_retry_after = document.get("retry_after")
        if (
            isinstance(body_retry_after, (int, float))
            and not isinstance(body_retry_after, bool)
            and body_retry_after >= 0
        ):
            retry_after = body_retry_after
    return CliError(
        "rate_limited",
        "Discord rate limit exceeded",
        5,
        retry_after=retry_after,
    )


def api_call(
    base: str,
    token: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params)}"
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bot {token}",
        "User-Agent": USER_AGENT,
    }
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirects).open(
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            return parse_json(read_response_body(response))
    except HTTPError as error:
        if error.code == 429:
            raise rate_limit_error(error) from error
        raw = read_response_body(error)
        if error.code == 401:
            raise CliError(
                "unauthorized", "Discord API request failed", 1
            ) from error
        try:
            document = parse_object(raw)
        except CliError as parse_error:
            if parse_error.code == "response_too_large":
                raise
            raise CliError(
                "api_error", "Discord API request failed", 1
            ) from parse_error
        raise CliError(
            provider_error_code(document, error.code),
            "Discord API request failed",
            1,
        ) from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise CliError("network_error", "Discord API request failed", 3) from error


def parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="mindalert-discord")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("auth-test", help="verify Discord authentication")

    history = commands.add_parser("history", help="read channel messages")
    history.add_argument("--channel", required=True)
    history.add_argument("--limit", type=int, default=50)
    position = history.add_mutually_exclusive_group()
    position.add_argument("--before")
    position.add_argument("--after")

    commands.add_parser("send", help="send stdin JSON to Discord")
    return root


def valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER_RE.fullmatch(value))


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    token_option = any(
        argument == "--token" or argument.startswith("--token=") for argument in argv
    )
    if token_option:
        raise CliError("invalid_arguments", "token options are not supported", 2)
    arguments = parser().parse_args(argv)
    if arguments.command == "history":
        if not 1 <= arguments.limit <= 100:
            raise CliError("invalid_arguments", "--limit must be between 1 and 100", 2)
        if not valid_identifier(arguments.channel):
            raise CliError("invalid_arguments", "invalid --channel", 2)
        if arguments.before is not None and not valid_identifier(arguments.before):
            raise CliError("invalid_arguments", "invalid --before", 2)
        if arguments.after is not None and not valid_identifier(arguments.after):
            raise CliError("invalid_arguments", "invalid --after", 2)
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
    if set(payload) - {"channel_id", "content", "reply_to_message_id"}:
        raise CliError("invalid_input", "stdin contains unsupported fields", 2)

    channel_id = payload.get("channel_id")
    content = payload.get("content")
    reply_id = payload.get("reply_to_message_id")
    if not valid_identifier(channel_id):
        raise CliError("invalid_input", "channel_id must be a valid identifier", 2)
    if not isinstance(content, str) or not content:
        raise CliError("invalid_input", "content must be a non-empty string", 2)
    if len(content) > 2000:
        raise CliError("content_too_long", "content exceeds 2000 characters", 2)
    if reply_id is not None and not valid_identifier(reply_id):
        raise CliError(
            "invalid_input", "reply_to_message_id must be a valid identifier", 2
        )

    result = {"channel_id": channel_id, "content": content}
    if reply_id is not None:
        result["reply_to_message_id"] = reply_id
    return result


def auth_result(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise CliError("invalid_response", "Discord returned an invalid user", 1)
    return {
        "ok": True,
        "id": document.get("id"),
        "username": document.get("username"),
    }


def send_result(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise CliError("invalid_response", "Discord returned an invalid message", 1)
    result = {
        "ok": True,
        "id": document.get("id"),
        "channel_id": document.get("channel_id"),
        "content": document.get("content"),
    }
    reference = document.get("message_reference")
    if isinstance(reference, dict):
        result["message_reference"] = reference
    return result


def history_result(document: Any, limit: int) -> dict[str, Any]:
    if not isinstance(document, list) or not all(
        isinstance(message, dict) for message in document
    ):
        raise CliError(
            "invalid_response", "Discord returned an invalid message list", 1
        )
    truncated = len(document) == limit
    result: dict[str, Any] = {
        "ok": True,
        "messages": document,
        "truncated": truncated,
    }
    if truncated:
        oldest_id = document[-1].get("id")
        if not isinstance(oldest_id, str) or not oldest_id:
            raise CliError("invalid_response", "Discord message has no id", 1)
        result["next_before"] = oldest_id
    return result


def run(argv: list[str]) -> dict[str, Any]:
    arguments = parse_arguments(argv)
    send_payload = read_send_payload() if arguments.command == "send" else None

    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise CliError("missing_credentials", f"set {TOKEN_ENV}", 4)
    base = validate_api_base(os.environ.get(API_BASE_ENV, DEFAULT_API_BASE))

    if arguments.command == "auth-test":
        return redact(auth_result(api_call(base, token, "users/@me")), token)
    if arguments.command == "send":
        assert send_payload is not None
        payload: dict[str, Any] = {
            "content": send_payload["content"],
            "allowed_mentions": {"parse": []},
        }
        reply_id = send_payload.get("reply_to_message_id")
        if reply_id is not None:
            payload["message_reference"] = {"message_id": reply_id}
        document = api_call(
            base,
            token,
            f"channels/{send_payload['channel_id']}/messages",
            payload=payload,
        )
        return redact(send_result(document), token)

    params: dict[str, Any] = {"limit": arguments.limit}
    if arguments.before is not None:
        params["before"] = arguments.before
    if arguments.after is not None:
        params["after"] = arguments.after
    document = api_call(
        base,
        token,
        f"channels/{arguments.channel}/messages",
        params=params,
    )
    return redact(history_result(document, arguments.limit), token)


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
