"""Minimal stdlib-only WhatsApp Business Cloud API sender for MindAlert."""

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
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_API_BASE = "https://graph.facebook.com/v23.0"
TOKEN_ENV = "WHATSAPP_ACCESS_TOKEN"
PHONE_ID_ENV = "WHATSAPP_PHONE_NUMBER_ID"
API_BASE_ENV = "WHATSAPP_API_BASE"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "mindalert-whatsapp/0.1.0"
PHONE_ID_RE = re.compile(r"[0-9]+\Z")
RECIPIENT_RE = re.compile(r"[+]?[0-9]{7,15}\Z")
SECRET_OPTIONS = ("--token", "--access-token")
SECRET_VALUES: list[str] = []


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


def remember_secret(value: str | None) -> None:
    if value and value not in SECRET_VALUES:
        SECRET_VALUES.append(value)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for secret in SECRET_VALUES:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            redact(key) if isinstance(key, str) else key: redact(item)
            for key, item in value.items()
        }
    return value


def emit_json(document: Any, stream) -> None:
    json.dump(redact(document), stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


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
    if hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
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
                    "WhatsApp response exceeded the 8 MiB limit",
                    3,
                )
        except ValueError:
            pass
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(
            "response_too_large",
            "WhatsApp response exceeded the 8 MiB limit",
            3,
        )
    return raw


def parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_response", "WhatsApp returned invalid JSON", 1) from error


def meta_error_code(raw: bytes) -> int | None:
    try:
        document = parse_json(raw)
    except CliError as error:
        if error.code == "response_too_large":
            raise
        return None
    if not isinstance(document, dict):
        return None
    provider_error = document.get("error")
    if not isinstance(provider_error, dict):
        return None
    code = provider_error.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def retry_after(headers) -> int | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return max(seconds, 0)


def api_error(raw: bytes, status: int, headers) -> CliError:
    code = meta_error_code(raw)
    if status == 429 or code in {130429, 80007}:
        return CliError(
            "rate_limited",
            "WhatsApp rate limit exceeded",
            5,
            retry_after=retry_after(headers),
        )
    error_code = f"whatsapp_{code}" if code is not None else "api_error"
    return CliError(error_code, "WhatsApp API request failed", 1)


def api_call(
    base: str,
    token: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{base}/{path.lstrip('/')}"
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
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
        raw = read_response_body(error)
        raise api_error(raw, error.code, error.headers) from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise CliError("network_error", "WhatsApp API request failed", 3) from error


def parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="mindalert-whatsapp")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("auth-test", help="verify WhatsApp Cloud API credentials")
    commands.add_parser("send", help="send stdin JSON through WhatsApp")
    commands.add_parser("history", help="report that message history is unsupported")
    return root


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    if any(
        argument == option or argument.startswith(f"{option}=")
        for argument in argv
        for option in SECRET_OPTIONS
    ):
        raise CliError("invalid_arguments", "secret options are not supported", 2)
    return parser().parse_args(argv)


def check_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise CliError("invalid_input", "stdin contains unsupported fields", 2)


def read_object() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_input", "stdin must contain one JSON object", 2) from error
    if not isinstance(payload, dict):
        raise CliError("invalid_input", "stdin must contain one JSON object", 2)
    return payload


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def send_payload(source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    check_fields(source, {"to", "text", "template", "reply_to_message_id"})
    recipient = source.get("to")
    if not isinstance(recipient, str) or not RECIPIENT_RE.fullmatch(recipient):
        raise CliError("invalid_input", "to must be a valid phone number", 2)
    recipient = recipient.removeprefix("+")

    has_text = "text" in source
    has_template = "template" in source
    if has_text == has_template:
        raise CliError("invalid_input", "provide exactly one of text or template", 2)

    result: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient,
    }
    if has_text:
        text = source.get("text")
        if not isinstance(text, str) or not text:
            raise CliError("invalid_input", "text must be a non-empty string", 2)
        if len(text) > 4096:
            raise CliError("content_too_long", "text exceeds 4096 characters", 2)
        result.update({"type": "text", "text": {"body": text}})
    else:
        template = source.get("template")
        if not isinstance(template, dict):
            raise CliError("invalid_input", "template must be an object", 2)
        check_fields(template, {"name", "language", "components"})
        name = template.get("name")
        language = template.get("language")
        components = template.get("components")
        if not nonempty_string(name) or not nonempty_string(language):
            raise CliError(
                "invalid_input",
                "template name and language must be non-empty strings",
                2,
            )
        if "components" in template and not isinstance(components, list):
            raise CliError("invalid_input", "template components must be a list", 2)
        template_body: dict[str, Any] = {
            "name": name,
            "language": {"code": language},
        }
        if "components" in template:
            template_body["components"] = components
        result.update({"type": "template", "template": template_body})

    reply_id = source.get("reply_to_message_id")
    if "reply_to_message_id" in source:
        if not nonempty_string(reply_id):
            raise CliError(
                "invalid_input",
                "reply_to_message_id must be a non-empty string",
                2,
            )
        result["context"] = {"message_id": reply_id}
    return recipient, result


def credentials() -> tuple[str, str, str]:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise CliError("missing_credentials", f"set {TOKEN_ENV}", 4)
    phone_id = os.environ.get(PHONE_ID_ENV)
    if phone_id is None:
        raise CliError("missing_credentials", f"set {PHONE_ID_ENV}", 4)
    if not PHONE_ID_RE.fullmatch(phone_id):
        raise CliError("invalid_credentials", f"invalid {PHONE_ID_ENV}", 2)
    base = validate_api_base(os.environ.get(API_BASE_ENV, DEFAULT_API_BASE))
    return base, token, phone_id


def auth_result(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise CliError("invalid_response", "WhatsApp returned an invalid phone", 1)
    return {
        "ok": True,
        "id": document.get("id"),
        "display_phone_number": document.get("display_phone_number"),
        "verified_name": document.get("verified_name"),
        "quality_rating": document.get("quality_rating"),
    }


def message_id(document: Any) -> str:
    if not isinstance(document, dict):
        raise CliError("invalid_response", "WhatsApp returned an invalid message", 1)
    messages = document.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        raise CliError("invalid_response", "WhatsApp response has no message", 1)
    identifier = messages[0].get("id")
    if not isinstance(identifier, str) or not identifier:
        raise CliError("invalid_response", "WhatsApp response has no message id", 1)
    return identifier


def run(argv: list[str]) -> dict[str, Any]:
    arguments = parse_arguments(argv)
    if arguments.command == "history":
        raise CliError(
            "unsupported",
            "WhatsApp Cloud API provides incoming messages only through webhooks",
            2,
        )
    source = read_object() if arguments.command == "send" else None
    base, token, phone_id = credentials()
    if arguments.command == "auth-test":
        return auth_result(api_call(base, token, phone_id))
    assert source is not None
    recipient, payload = send_payload(source)
    document = api_call(base, token, f"{phone_id}/messages", payload=payload)
    return {"ok": True, "message_id": message_id(document), "to": recipient}


def main() -> int:
    remember_secret(os.environ.get(TOKEN_ENV))
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
