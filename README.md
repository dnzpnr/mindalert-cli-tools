# mindalert-cli-tools

Stdlib-only (no third-party dependencies) Python CLI tools used by MindAlert team-agent.
Installed through [cli_tool_catalog](https://github.com/dnzpnr/cli_tool_catalog); each release asset is a `tar.gz` holding one executable zipapp.

## Build

Python 3.10 or newer is required. Build any tool from its directory under `tools/`:

```console
python3 build.py mindalert-slack
```

This creates `dist/mindalert-slack-0.1.0.tar.gz` and its matching `.sha256` file. The archive contains one executable named `mindalert-slack`. Builds are reproducible from identical sources.

## `mindalert-slack`

Set the bot token only through `SLACK_BOT_TOKEN`; token command-line options are rejected. `SLACK_API_BASE` is optional and defaults to `https://slack.com/api`. Plain HTTP is accepted only for a loopback address, for local testing.

```console
export SLACK_BOT_TOKEN='...'
mindalert-slack auth-test
mindalert-slack history --channel C123 --limit 100
mindalert-slack history --channel C123 --thread 1700000003.000300
printf '%s\n' '{"channel":"C123","text":"hello","thread_ts":"1700000003.000300"}' | mindalert-slack send
```

Successful commands write one JSON document to stdout. Errors leave stdout empty and write one JSON document to stderr.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 1 | Slack API rejection |
| 2 | Usage or configuration error |
| 3 | Network error |
| 4 | Missing credentials (`SLACK_BOT_TOKEN`) |
| 5 | Rate limited; `retry_after` reports seconds |

Slack API response bodies are limited to 8 MiB. A larger response fails with
exit 3 and `response_too_large`; response bodies are never printed verbatim.

## `mindalert-discord`

Set the bot token only through `DISCORD_BOT_TOKEN`; token command-line options
are rejected. `DISCORD_API_BASE` is optional and defaults to
`https://discord.com/api/v10`. Plain HTTP is accepted only for a loopback
address, for local testing. Redirects are not followed.

```console
export DISCORD_BOT_TOKEN='...'
mindalert-discord auth-test
mindalert-discord history --channel 123456789012345678 --limit 100
mindalert-discord history --channel 123456789012345678 --before 987654321098765432
printf '%s\n' '{"channel_id":"123456789012345678","content":"hello","reply_to_message_id":"987654321098765432"}' | mindalert-discord send
```

Successful commands write one JSON document to stdout. Errors leave stdout
empty and write one JSON document to stderr.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 1 | Discord API rejection |
| 2 | Usage or configuration error |
| 3 | Network error or response larger than 8 MiB |
| 4 | Missing credentials (`DISCORD_BOT_TOKEN`) |
| 5 | Rate limited; `retry_after` reports seconds |

Discord requests use the required `DiscordBot (URL, version)` User-Agent.
History accepts limits from 1 through 100 and exposes a possibly incomplete
page as `truncated: true` with `next_before`; Discord message fields such as
`message_reference` and `thread` are preserved. Threads use their thread ID as
the channel ID. Sending is limited to 2000 characters, never truncates content,
and disables all mentions with `allowed_mentions: {"parse": []}`. Replies rely
on Discord's default `fail_if_not_exists: true`, so a missing parent cannot
silently turn into a non-reply. Discord API response bodies are limited to
8 MiB and are never printed verbatim.

## `mindalert-graph`

`mindalert-graph` reads Outlook mail and Teams channel messages through
Microsoft Graph. It uses either a direct `MS_GRAPH_ACCESS_TOKEN`, or the
client-credentials flow with all three of `MS_TENANT_ID`, `MS_CLIENT_ID`, and
`MS_CLIENT_SECRET`. Tokens and secrets are accepted only through environment
variables; secret command-line options are rejected.

`MS_GRAPH_API_BASE` defaults to `https://graph.microsoft.com/v1.0`, and
`MS_LOGIN_BASE` defaults to `https://login.microsoftonline.com`. These are
primarily test overrides. Plain HTTP is accepted only for loopback addresses,
and redirects are not followed.

```console
export MS_TENANT_ID='...'
export MS_CLIENT_ID='...'
export MS_CLIENT_SECRET='...'
mindalert-graph auth-test
mindalert-graph mail-list --user alerts@example.com --folder inbox --top 50
mindalert-graph mail-list --user alerts@example.com --body
printf '%s\n' '{"user":"alerts@example.com","to":["oncall@example.com"],"subject":"Alert","body":"Disk full"}' | mindalert-graph mail-send
printf '%s\n' '{"user":"alerts@example.com","message_id":"AAMk...","comment":"Acknowledged"}' | mindalert-graph mail-reply
mindalert-graph teams-read --team TEAM_ID --channel '19:...@thread.tacv2' --top 50
mindalert-graph teams-read --team TEAM_ID --channel '19:...@thread.tacv2' --thread MESSAGE_ID
export TEAMS_WEBHOOK_URL='https://...'
printf '%s\n' '{"text":"Disk full"}' | mindalert-graph teams-post
```

`mail-send` also accepts an optional `cc` list and `body_type` set to `text`
or `html`. List commands report incomplete pages as `truncated: true` with a
full `next_cursor`; pass that value back with `--cursor`. A cursor must remain
under the configured Graph API origin and path prefix.

The Entra application needs these Microsoft Graph **application** permissions,
with administrator consent:

- `Mail.Read` for `mail-list`.
- `Mail.Send` for `mail-send` and `mail-reply`.
- `ChannelMessage.Read.All` for `teams-read`. This is a Microsoft protected
  Teams API permission and requires a protected-API access request in addition
  to tenant administrator consent.

`teams-post` deliberately uses a Teams Workflows webhook instead of Graph.
Graph's `ChannelMessage.Send` permission is delegated-only; application
permission can post channel messages only through the migration-only
`Teamwork.Migrate.All` flow. Set `TEAMS_WEBHOOK_URL` to the Workflows webhook
URL. Its `sig` query value is treated as a secret and is never printed.

Successful commands write one JSON document to stdout. Errors leave stdout
empty and write one JSON document to stderr.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 1 | Microsoft Graph, identity, or Teams webhook rejection |
| 2 | Usage or configuration error |
| 3 | Network error or response larger than 8 MiB |
| 4 | Missing credentials |
| 5 | Rate limited; `retry_after` reports seconds |

All Graph, identity, and webhook response bodies are limited to 8 MiB and are
never printed verbatim. Access tokens, client secrets, and Teams webhook `sig`
values are redacted from every output path.

## `mindalert-whatsapp`

`mindalert-whatsapp` is a sender-only client for the official WhatsApp Business
Cloud API. Set `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in the
environment. `WHATSAPP_API_BASE` is optional and defaults to
`https://graph.facebook.com/v23.0`; plain HTTP is accepted only for a loopback
address used by local tests. Redirects are not followed.

```console
export WHATSAPP_ACCESS_TOKEN='...'
export WHATSAPP_PHONE_NUMBER_ID='...'
mindalert-whatsapp auth-test
printf '%s\n' '{"to":"905550000001","text":"Disk full"}' | mindalert-whatsapp send
printf '%s\n' '{"to":"905550000001","template":{"name":"disk_alarm","language":"tr"}}' | mindalert-whatsapp send
mindalert-whatsapp history
```

`send` accepts either non-empty `text` (at most 4096 characters) or a
`template`, but not both. A template has `name`, `language`, and optional
`components`. Optional `reply_to_message_id` is sent as WhatsApp reply context.
Tokens are accepted only through the environment; token command-line options
are rejected.

`history` deliberately reports `unsupported` instead of returning an empty
list. Cloud API delivers incoming messages only through
[webhooks](https://developers.facebook.com/docs/whatsapp/cloud-api), so a
sender-only CLI cannot fetch message history. Free-form text may be sent only
inside the 24-hour customer service window. Outside that window, use an
approved template; Meta error `131047` indicates this re-engagement rule. See
Meta's [send-message and customer-service-window documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages#customer-service-windows).

Meta currently lists newer Graph API versions, but `v23.0` remains available
through October 8, 2027; this tool intentionally pins the contract's version.
See Meta's [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog).

Successful commands write one JSON document to stdout. Errors leave stdout
empty and write one JSON document to stderr.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 1 | WhatsApp Cloud API rejection (`whatsapp_<code>` when Meta supplies a code) |
| 2 | Usage, configuration, or unsupported-operation error |
| 3 | Network error or response larger than 8 MiB |
| 4 | Missing `WHATSAPP_ACCESS_TOKEN` or `WHATSAPP_PHONE_NUMBER_ID` |
| 5 | Rate limited; `retry_after` is included when Meta sends `Retry-After` |

WhatsApp response bodies are limited to 8 MiB and are never printed verbatim.
In particular, Meta's `error.message` is never copied to output because it may
echo an access token.
