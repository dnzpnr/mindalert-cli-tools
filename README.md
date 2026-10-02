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
