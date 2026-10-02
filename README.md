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
