# Integration boundaries

Job Radar runs without live integrations when using the synthetic demo database. Every optional integration is free to use from the application side and stores its state locally.

## Public ATS adapters

The code includes bounded read-only adapters for several public ATS formats. The tracked registry contains disabled fictional entries. To use a real documented public feed, copy `config/job_sources.example.json` to an ignored local file, review the provider's terms, and configure only public identifiers. Public collection uses timeouts, size and page limits, and conservative scheduling.

## Telegram

Telegram collection uses Telethon with a local session and reads only an explicitly configured channel. It does not send messages, join channels, react, or modify content. Credentials and session files must remain local and ignored. Native JSON exports can be previewed before import.

## Email recommendations

Amazon and LinkedIn recommendation parsers accept user-provided email or clipboard content. Optional Gmail access uses a separate local OAuth client and token with read-only processing. Authenticated job pages are not scraped or automated.

## WhatsApp notifications and exports

WhatsApp ingestion is limited to native exports or an allowlisted local Windows notification companion. It does not automate the WhatsApp client, send messages, or read chat history through an undocumented API. See [WINDOWS_NOTIFICATION_COMPANION.md](WINDOWS_NOTIFICATION_COMPANION.md).

## Safety defaults

- Live integrations are never run by tests or CI.
- Automatic public collection is disabled by default.
- Source errors can be isolated without discarding committed local data.
- Dashboard imports, preference changes, feedback, and refresh controls are loopback-only.
