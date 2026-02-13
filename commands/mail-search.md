---
description: Search emails by sender, subject, date, or text
argument-hint: [search query]
allowed-tools: ["mcp__imap-connector__search_emails", "mcp__imap-connector__read_email"]
---

Search emails based on the user's query: $ARGUMENTS

Translate the user's natural language request into structured IMAP search criteria:
- "emails from Max" → `from:Max`
- "about training" → `subject:training`
- "since last week" → `since:YYYY-MM-DD` (calculate the date)
- "before January" → `before:YYYY-MM-DD`
- Combine multiple criteria as needed.

1. Call `search_emails` with the appropriate query.
2. Present matching emails in a concise list: sender, subject, date.
3. Report how many results were found.
4. Offer to read any specific email in full.

If no results are found, suggest broadening the search or trying alternative keywords.
