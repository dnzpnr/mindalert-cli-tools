"""Minimal stdlib-only Microsoft Graph and Teams Workflows CLI for MindAlert."""

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
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE_ENV = "MS_GRAPH_API_BASE"
LOGIN_BASE_ENV = "MS_LOGIN_BASE"
TOKEN_ENV = "MS_GRAPH_ACCESS_TOKEN"
TENANT_ENV = "MS_TENANT_ID"
CLIENT_ENV = "MS_CLIENT_ID"
CLIENT_SECRET_ENV = "MS_CLIENT_SECRET"
WEBHOOK_ENV = "TEAMS_WEBHOOK_URL"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "mindalert-graph/0.1.0"
PATH_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@%-]*\Z")
EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z"
)
SECRET_OPTIONS = ("--client-secret", "--token", "--access-token", "--webhook-url")
SECRET_VALUES: list[str] = []


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


def remember_secret(value: str | None) -> None:
    if value and value not in SECRET_VALUES:
        SECRET_VALUES.append(value)


def remember_environment_secrets() -> None:
    remember_secret(os.environ.get(TOKEN_ENV))
    remember_secret(os.environ.get(CLIENT_SECRET_ENV))
    webhook = os.environ.get(WEBHOOK_ENV)
    if not webhook:
        return
    try:
        values = parse_qs(urlsplit(webhook).query, keep_blank_values=True).get("sig", [])
    except ValueError:
        values = []
    for value in values:
        remember_secret(value)


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


def validate_url(raw_url: str, env_name: str, error_code: str, *, query: bool) -> str:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as error:
        raise CliError(error_code, f"invalid {env_name}", 2) from error
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not query)
    ):
        raise CliError(error_code, f"invalid {env_name}", 2)
    if port is not None and not 1 <= port <= 65535:
        raise CliError(error_code, f"invalid {env_name}", 2)
    if parsed.scheme == "https":
        return raw_url if query else raw_url.rstrip("/")
    if parsed.scheme != "http":
        raise CliError(error_code, f"{env_name} must use HTTPS", 2)
    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise CliError(
                error_code,
                f"HTTP {env_name} must use a loopback address",
                2,
            )
    return raw_url if query else raw_url.rstrip("/")


def validate_api_base(raw_url: str, env_name: str) -> str:
    return validate_url(raw_url, env_name, "insecure_api_base", query=False)


def validate_webhook_url(raw_url: str) -> str:
    return validate_url(raw_url, WEBHOOK_ENV, "insecure_webhook_url", query=True)


def url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def validate_cursor(cursor: str, base: str) -> str:
    try:
        parsed = urlsplit(cursor)
        base_parsed = urlsplit(base)
        cursor_origin = url_origin(cursor)
        base_origin = url_origin(base)
    except ValueError as error:
        raise CliError("invalid_cursor", "cursor is outside the Graph API base", 2) from error
    base_path = unquote(base_parsed.path).rstrip("/")
    cursor_path = unquote(parsed.path)
    within_path = cursor_path == base_path or cursor_path.startswith(f"{base_path}/")
    has_dot_segment = any(segment in {".", ".."} for segment in cursor_path.split("/"))
    if (
        cursor_origin != base_origin
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not within_path
        or has_dot_segment
    ):
        raise CliError("invalid_cursor", "cursor is outside the Graph API base", 2)
    return cursor


def read_response_body(response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise CliError(
                    "response_too_large",
                    "HTTP response exceeded the 8 MiB limit",
                    3,
                )
        except ValueError:
            pass
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(
            "response_too_large",
            "HTTP response exceeded the 8 MiB limit",
            3,
        )
    return raw


def parse_json(raw: bytes, service: str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_response", f"{service} returned invalid JSON", 1) from error


def retry_after(headers) -> int:
    try:
        value = int(headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def provider_error(raw: bytes, service: str, status: int) -> CliError:
    if service == "Teams webhook":
        return CliError(
            "webhook_rejected",
            f"Teams webhook rejected the request with HTTP {status}",
            1,
        )
    try:
        document = parse_json(raw, service)
    except CliError:
        return CliError("api_error", f"{service} request failed", 1)
    code: Any = None
    if isinstance(document, dict):
        error = document.get("error")
        if isinstance(error, dict):
            code = error.get("code")
        elif isinstance(error, str):
            code = error
    if not isinstance(code, str) or not code:
        code = "api_error"
    return CliError(code, f"{service} request failed", 1)


def http_request(
    url: str,
    service: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with build_opener(NoRedirects).open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, read_response_body(response)
    except HTTPError as error:
        raw = read_response_body(error)
        if error.code == 429:
            raise CliError(
                "rate_limited",
                f"{service} rate limit exceeded",
                5,
                retry_after=retry_after(error.headers),
            ) from error
        raise provider_error(raw, service, error.code) from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise CliError("network_error", f"{service} request failed", 3) from error


def path_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(PATH_IDENTIFIER_RE.fullmatch(value))


def encoded_identifier(value: str) -> str:
    return quote(value, safe=":@")


def require_path_identifier(value: Any, name: str) -> str:
    if not path_identifier(value):
        raise CliError("invalid_input", f"{name} must be a valid identifier", 2)
    return value


def email_address(value: Any) -> bool:
    return isinstance(value, str) and bool(EMAIL_RE.fullmatch(value))


def graph_bases() -> tuple[str, str]:
    graph_base = validate_api_base(
        os.environ.get(GRAPH_BASE_ENV, DEFAULT_GRAPH_BASE), GRAPH_BASE_ENV
    )
    login_base = validate_api_base(
        os.environ.get(LOGIN_BASE_ENV, DEFAULT_LOGIN_BASE), LOGIN_BASE_ENV
    )
    return graph_base, login_base


def access_token(login_base: str) -> tuple[str, dict[str, Any]]:
    direct = os.environ.get(TOKEN_ENV)
    if direct:
        remember_secret(direct)
        return direct, {"ok": True}
    tenant = os.environ.get(TENANT_ENV)
    client = os.environ.get(CLIENT_ENV)
    client_secret = os.environ.get(CLIENT_SECRET_ENV)
    if not tenant or not client or not client_secret:
        raise CliError(
            "missing_credentials",
            f"set {TOKEN_ENV} or {TENANT_ENV}, {CLIENT_ENV}, and {CLIENT_SECRET_ENV}",
            4,
        )
    require_path_identifier(tenant, TENANT_ENV)
    if not path_identifier(client):
        raise CliError("invalid_credentials", f"invalid {CLIENT_ENV}", 2)
    remember_secret(client_secret)
    form = urlencode(
        {
            "client_id": client,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("ascii")
    url = f"{login_base}/{encoded_identifier(tenant)}/oauth2/v2.0/token"
    _, raw = http_request(
        url,
        "Microsoft identity",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=form,
    )
    document = parse_json(raw, "Microsoft identity")
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("access_token"), str)
        or not document["access_token"]
    ):
        raise CliError("invalid_response", "Microsoft identity returned no access token", 1)
    token = document["access_token"]
    remember_secret(token)
    result: dict[str, Any] = {"ok": True}
    expires_in = document.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        result["expires_in"] = expires_in
    return token, result


def graph_request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    _, raw = http_request(url, "Microsoft Graph", method=method, headers=headers, body=body)
    if not raw:
        return None
    return parse_json(raw, "Microsoft Graph")


def parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="mindalert-graph")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("auth-test", help="verify Microsoft credentials")

    mail_list = commands.add_parser("mail-list", help="list Outlook messages")
    mail_list.add_argument("--user", required=True)
    mail_list.add_argument("--folder", default="inbox")
    mail_list.add_argument("--top", type=int, default=50)
    mail_list.add_argument("--cursor")
    mail_list.add_argument("--body", action="store_true")

    commands.add_parser("mail-send", help="send an Outlook message from stdin JSON")
    commands.add_parser("mail-reply", help="reply to an Outlook message from stdin JSON")

    teams_read = commands.add_parser("teams-read", help="read Teams channel messages")
    teams_read.add_argument("--team", required=True)
    teams_read.add_argument("--channel", required=True)
    teams_read.add_argument("--thread")
    teams_read.add_argument("--top", type=int, default=50)
    teams_read.add_argument("--cursor")

    commands.add_parser("teams-post", help="post stdin JSON through a Teams Workflow")
    return root


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    for argument in argv:
        if any(argument == option or argument.startswith(f"{option}=") for option in SECRET_OPTIONS):
            raise CliError("invalid_arguments", "secret options are not supported", 2)
    arguments = parser().parse_args(argv)
    if arguments.command == "mail-list":
        require_path_identifier(arguments.user, "--user")
        require_path_identifier(arguments.folder, "--folder")
        if not 1 <= arguments.top <= 1000:
            raise CliError("invalid_arguments", "--top must be between 1 and 1000", 2)
    if arguments.command == "teams-read":
        require_path_identifier(arguments.team, "--team")
        require_path_identifier(arguments.channel, "--channel")
        if arguments.thread is not None:
            require_path_identifier(arguments.thread, "--thread")
        if not 1 <= arguments.top <= 50:
            raise CliError("invalid_arguments", "--top must be between 1 and 50", 2)
    return arguments


def read_object() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_input", "stdin must contain one JSON object", 2) from error
    if not isinstance(payload, dict):
        raise CliError("invalid_input", "stdin must contain one JSON object", 2)
    return payload


def check_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise CliError("invalid_input", "stdin contains unsupported fields", 2)


def recipients(value: Any, name: str, *, required: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (required and not value):
        raise CliError("invalid_input", f"{name} must be a non-empty list", 2)
    if not all(email_address(address) for address in value):
        raise CliError("invalid_input", f"{name} contains an invalid email address", 2)
    return [{"emailAddress": {"address": address}} for address in value]


def mail_send_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    check_fields(payload, {"user", "to", "cc", "subject", "body", "body_type"})
    user = require_path_identifier(payload.get("user"), "user")
    subject = payload.get("subject")
    body = payload.get("body")
    body_type = payload.get("body_type", "text")
    if not isinstance(subject, str) or not subject:
        raise CliError("invalid_input", "subject must be a non-empty string", 2)
    if not isinstance(body, str):
        raise CliError("invalid_input", "body must be a string", 2)
    if not isinstance(body_type, str) or body_type.lower() not in {"text", "html"}:
        raise CliError("invalid_input", "body_type must be text or html", 2)
    to_recipients = recipients(payload.get("to"), "to", required=True)
    cc_recipients = recipients(payload.get("cc", []), "cc", required=False)
    message = {
        "subject": subject,
        "body": {"contentType": body_type.upper() if body_type.lower() == "html" else "Text", "content": body},
        "toRecipients": to_recipients,
        "ccRecipients": cc_recipients,
    }
    return user, {"message": message, "saveToSentItems": True}


def mail_reply_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    check_fields(payload, {"user", "message_id", "comment"})
    user = require_path_identifier(payload.get("user"), "user")
    message_id = require_path_identifier(payload.get("message_id"), "message_id")
    comment = payload.get("comment")
    if not isinstance(comment, str) or not comment:
        raise CliError("invalid_input", "comment must be a non-empty string", 2)
    return user, message_id, {"comment": comment}


def page_result(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise CliError("invalid_response", "Microsoft Graph returned an invalid page", 1)
    messages = document.get("value")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise CliError("invalid_response", "Microsoft Graph response has no message list", 1)
    next_cursor = document.get("@odata.nextLink")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise CliError("invalid_response", "Microsoft Graph returned an invalid next cursor", 1)
    result: dict[str, Any] = {
        "ok": True,
        "messages": messages,
        "truncated": next_cursor is not None,
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def graph_context() -> tuple[str, str, dict[str, Any]]:
    graph_base, login_base = graph_bases()
    token, auth = access_token(login_base)
    return graph_base, token, auth


def mail_list(arguments: argparse.Namespace, base: str, token: str) -> dict[str, Any]:
    if arguments.cursor:
        url = validate_cursor(arguments.cursor, base)
    else:
        fields = [
            "id",
            "conversationId",
            "subject",
            "from",
            "toRecipients",
            "ccRecipients",
            "receivedDateTime",
            "isRead",
            "hasAttachments",
            "bodyPreview",
            "webLink",
        ]
        if arguments.body:
            fields.append("body")
        path = (
            f"users/{encoded_identifier(arguments.user)}/mailFolders/"
            f"{encoded_identifier(arguments.folder)}/messages"
        )
        query = urlencode(
            {
                "$select": ",".join(fields),
                "$orderby": "receivedDateTime desc",
                "$top": arguments.top,
            }
        )
        url = f"{base}/{path}?{query}"
    return page_result(graph_request(url, token))


def teams_read(arguments: argparse.Namespace, base: str, token: str) -> dict[str, Any]:
    if arguments.cursor:
        url = validate_cursor(arguments.cursor, base)
    else:
        path = (
            f"teams/{encoded_identifier(arguments.team)}/channels/"
            f"{encoded_identifier(arguments.channel)}/messages"
        )
        if arguments.thread is not None:
            path = f"{path}/{encoded_identifier(arguments.thread)}/replies"
        url = f"{base}/{path}?{urlencode({'$top': arguments.top})}"
    return page_result(graph_request(url, token))


def teams_post(payload: dict[str, Any]) -> dict[str, Any]:
    check_fields(payload, {"text"})
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise CliError("invalid_input", "text must be a non-empty string", 2)
    webhook = os.environ.get(WEBHOOK_ENV)
    if not webhook:
        raise CliError("missing_credentials", f"set {WEBHOOK_ENV}", 4)
    url = validate_webhook_url(webhook)
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }
    body = json.dumps(card, separators=(",", ":")).encode("utf-8")
    status, _ = http_request(
        url,
        "Teams webhook",
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=body,
    )
    return {"ok": True, "status": status}


def run(argv: list[str]) -> dict[str, Any]:
    arguments = parse_arguments(argv)
    stdin_payload = None
    if arguments.command in {"mail-send", "mail-reply", "teams-post"}:
        stdin_payload = read_object()
    if arguments.command == "teams-post":
        assert stdin_payload is not None
        return teams_post(stdin_payload)

    base, token, auth = graph_context()
    if arguments.command == "auth-test":
        return auth
    if arguments.command == "mail-list":
        return mail_list(arguments, base, token)
    if arguments.command == "teams-read":
        return teams_read(arguments, base, token)
    if arguments.command == "mail-send":
        assert stdin_payload is not None
        user, payload = mail_send_payload(stdin_payload)
        url = f"{base}/users/{encoded_identifier(user)}/sendMail"
        graph_request(url, token, method="POST", payload=payload)
        return {"ok": True}
    assert arguments.command == "mail-reply" and stdin_payload is not None
    user, message_id, payload = mail_reply_payload(stdin_payload)
    url = (
        f"{base}/users/{encoded_identifier(user)}/messages/"
        f"{encoded_identifier(message_id)}/reply"
    )
    graph_request(url, token, method="POST", payload=payload)
    return {"ok": True}


def main() -> int:
    remember_environment_secrets()
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
