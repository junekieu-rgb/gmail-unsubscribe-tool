# Gmail Unsubscribe Tool

Scans your Gmail Promotions category for marketing emails, groups them by
sender, and lets you unsubscribe one sender at a time with a y/n prompt.
Nothing happens automatically — every unsubscribe action requires your
explicit confirmation.

How it works:
- Only acts on senders that include a standard `List-Unsubscribe` header
  (RFC 2369 / 8058) — the same mechanism Gmail's own "Unsubscribe" button
  uses. It does not scrape or click links inside the email body, since those
  are more likely to be trackers or unreliable.
- For each sender you approve, it either sends a one-click POST / GET to
  their unsubscribe URL, or sends a blank "unsubscribe" email to their
  unsubscribe address (mailto), whichever the sender provides.
- After a successful unsubscribe, it can also offer to move that sender's
  existing emails to Gmail's **Trash** (recoverable for 30 days) — it never
  permanently deletes anything.
- Keeps a local log (`unsubscribe_log.json`) so senders you've already
  handled aren't shown again on the next run.
- `--dry-run` doesn't send anything, but it does remember which senders you
  said "yes" to, so a later real run can bulk-confirm them instead of asking
  again one by one.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create a Google Cloud OAuth client so the script can access your Gmail:
   - Go to https://console.cloud.google.com/ and create (or select) a project.
   - Enable the **Gmail API** (APIs & Services → Library → search "Gmail API" → Enable).
   - Go to APIs & Services → Credentials → Create Credentials → OAuth client ID.
   - If prompted, configure the OAuth consent screen first (User type: External,
     add yourself as a test user — this app stays in "Testing" mode which is fine
     for personal use).
   - Application type: **Desktop app**. Create it, then download the JSON.
   - Save the downloaded file as `credentials.json` in this directory.

3. Run it:
   ```bash
   python unsubscribe.py --dry-run
   ```
   The first run opens a browser window to authorize access to your Gmail
   (read/modify — needed to Trash emails — plus send, needed for mailto-based
   unsubscribes). A `token.json` is saved so you won't need to re-authorize
   each time. If you're upgrading from an older version of this tool that
   only requested read-only access, delete `token.json` once so it can
   re-authorize with the new permissions.

## Usage

```bash
# Preview only, sends nothing
python unsubscribe.py --dry-run

# Real run: review and confirm each sender
python unsubscribe.py

# Scan more messages (default 300) — headers are fetched in batches of 50,
# so a few thousand messages takes a few minutes rather than tens of minutes
python unsubscribe.py --limit 5000

# Re-review senders already marked done/skipped in the log
python unsubscribe.py --rescan

# Scan the whole inbox instead of just Promotions
python unsubscribe.py --query "in:inbox"

# Go back through already-unsubscribed senders and offer to Trash their old mail
python unsubscribe.py --cleanup

# Check whether unsubscribed senders actually stopped emailing you
python unsubscribe.py --verify

# Auto-unsubscribe + Trash existing mail for every sender found, no per-sender
# prompts (still asks one confirmation up front, since this touches everything)
python unsubscribe.py --limit 22113 --yes
```

During review, for each sender you can answer:
- `y` — unsubscribe now
- `n` — skip for now (asked again next run)
- `never` — skip permanently (won't be shown again)
- `q` — stop reviewing

After a successful unsubscribe (or during `--cleanup`), you'll also be asked
whether to move that sender's existing emails to Trash — separate y/n, since
unsubscribing and deleting past mail are different decisions.

### How do I know it worked?

Two ways:
- `unsubscribe_log.json` records the outcome of every action (`done`,
  `failed`, `skipped-permanent`) with a timestamp and HTTP status/method.
- `python unsubscribe.py --verify` checks, for each sender marked `done`,
  whether any mail has arrived from them since the unsubscribe date. Senders
  can legally take time to honor a request (up to ~10 business days is
  common), so this is most useful a week or two after unsubscribing.

## Notes / limitations

- Senders that don't send a `List-Unsubscribe` header are skipped entirely —
  you'll need to unsubscribe from those manually.
- `credentials.json`, `token.json`, and `unsubscribe_log.json` contain
  account-specific data and are gitignored; don't share them.
- Gmail API quotas apply; scanning thousands of messages will be slow and
  may hit rate limits — use `--limit` to control scope.
