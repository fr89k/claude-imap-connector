---
description: Check inbox for recent emails
allowed-tools: ["mcp__imap-connector__list_emails", "mcp__imap-connector__read_email", "mcp__imap-connector__list_folders"]
---

Check the user's inbox for recent emails.

1. Call `list_emails` with the default INBOX folder to get the most recent messages.
2. Present a concise summary of each email: sender, subject, date.
3. If the user specifies a folder (e.g. "Sent", "Drafts"), use that folder instead.
4. Offer to read any specific email in detail if the user is interested.

Keep the output clean and scannable. Use a simple list format showing sender, subject, and relative time (e.g. "2 hours ago", "yesterday").
