#!/usr/bin/env python3
"""Scan Gmail for marketing emails, unsubscribe, and clean up old mail — all with
per-sender confirmation.

Setup: see README.md. Requires credentials.json from a Google Cloud OAuth
client (Desktop app type) in the same directory as this script.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "unsubscribe_log.json")

# gmail.modify covers reading + trashing/label changes, but NOT permanent delete.
# gmail.send is only needed for mailto: unsubscribe requests.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

HEADERS_TO_FETCH = ["From", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post"]
REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; unsubscribe-tool/1.0)"
TRASH_CHUNK_SIZE = 500
# Gmail's per-user limit is a fixed 250 quota units/second; messages.get costs 5 units,
# so BATCH_SIZE * 5 must stay safely under 250 since a batch's sub-requests land ~simultaneously.
BATCH_SIZE = 20  # 20 * 5 = 100 units/batch
BASE_BATCH_DELAY = 1.0  # seconds between batches, adaptively raised on quota errors
MAX_BATCH_DELAY = 20.0


def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            stored_scopes = set(json.load(f).get("scopes", []))
        if set(SCOPES).issubset(stored_scopes):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        # else: token predates a scope this run needs; leave creds None to force re-auth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                sys.exit(
                    f"Missing {CREDENTIALS_FILE}\n"
                    "Download OAuth 'Desktop app' credentials from Google Cloud "
                    "Console and save them there. See README.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {}


def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, sort_keys=True)


def parse_list_unsubscribe(value):
    """Return dict with 'mailto' and 'https' candidates from a List-Unsubscribe header."""
    result = {"mailto": None, "https": None}
    if not value:
        return result
    for part in re.findall(r"<([^>]+)>", value):
        if part.lower().startswith("mailto:") and not result["mailto"]:
            result["mailto"] = part[len("mailto:"):].split("?")[0]
        elif part.lower().startswith("http") and not result["https"]:
            result["https"] = part
    return result


def extract_email(from_header):
    match = re.search(r"<([^>]+)>", from_header or "")
    if match:
        return match.group(1).lower()
    return (from_header or "").strip().lower()


def header_value(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def method_label(entry):
    if entry["https"] and entry["one_click"]:
        return "https (one-click)"
    if entry["https"]:
        return "https"
    if entry["mailto"]:
        return "mailto"
    return "none"


RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError"}


def _is_retryable_http_error(e):
    status = getattr(e.resp, "status", None)
    if status not in RETRYABLE_STATUSES:
        return False
    if status != 403:
        return True
    try:
        reason = json.loads(e.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
    except Exception:
        reason = ""
    return reason in RETRYABLE_REASONS


def execute_with_retry(request, max_retries=6, base_delay=2):
    """Execute a single Gmail API request, retrying quota/transient errors with backoff.
    The client library's built-in num_retries only covers 5xx/429, not Gmail's 403
    'rateLimitExceeded' responses, so this handles that case explicitly."""
    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if not _is_retryable_http_error(e) or attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"  Gmail API error ({e.resp.status}), retrying in {delay}s...")
            time.sleep(delay)


def execute_batch_with_retry(batch, max_retries=5):
    """BatchHttpRequest.execute() has no built-in retry, unlike single requests."""
    delay = 1
    for attempt in range(max_retries + 1):
        try:
            batch.execute()
            return
        except Exception as e:
            if attempt == max_retries:
                raise
            print(f"  batch request failed ({e}), retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2


def scan(service, query, limit):
    print(f"Searching Gmail for: {query!r} (limit {limit} messages)")
    ids = list_message_ids(service, query, cap=limit)
    total = len(ids)
    print(f"Found {total} matching message(s). Fetching headers in batches of {BATCH_SIZE}...")

    senders = defaultdict(lambda: {
        "from_header": "",
        "count": 0,
        "subjects": [],
        "mailto": None,
        "https": None,
        "one_click": False,
    })
    fetched = 0
    failed_ids = []

    def handle_message(request_id, msg, exception):
        nonlocal fetched
        if exception is not None:
            if isinstance(exception, HttpError) and _is_retryable_http_error(exception):
                failed_ids.append(request_id)  # retried in a later pass below
            else:
                fetched += 1
                print(f"  warning: could not fetch message {request_id}: {exception}")
            return
        fetched += 1

        headers = msg.get("payload", {}).get("headers", [])
        list_unsub = header_value(headers, "List-Unsubscribe")
        if not list_unsub:
            return  # no standard unsubscribe header; skip (no body scraping)

        from_header = header_value(headers, "From") or ""
        email_addr = extract_email(from_header)
        subject = header_value(headers, "Subject") or ""
        one_click_post = header_value(headers, "List-Unsubscribe-Post") or ""

        parsed = parse_list_unsubscribe(list_unsub)
        entry = senders[email_addr]
        entry["from_header"] = from_header
        entry["count"] += 1
        if len(entry["subjects"]) < 3:
            entry["subjects"].append(subject)
        if parsed["mailto"] and not entry["mailto"]:
            entry["mailto"] = parsed["mailto"]
        if parsed["https"] and not entry["https"]:
            entry["https"] = parsed["https"]
        if "one-click" in one_click_post.lower():
            entry["one_click"] = True

    def fetch_ids(id_list, label):
        """Fetch metadata in batches, adaptively pacing to stay under Gmail's per-minute quota:
        slow down when a batch hits quota errors, ease back toward baseline when it doesn't."""
        delay = BASE_BATCH_DELAY
        for i in range(0, len(id_list), BATCH_SIZE):
            chunk = id_list[i:i + BATCH_SIZE]
            before = len(failed_ids)
            batch = service.new_batch_http_request(callback=handle_message)
            for mid in chunk:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=mid, format="metadata", metadataHeaders=HEADERS_TO_FETCH,
                    ),
                    request_id=mid,
                )
            execute_batch_with_retry(batch)
            print(f"  {label} {min(i + BATCH_SIZE, len(id_list))}/{len(id_list)}...")
            if len(failed_ids) > before:
                delay = min(delay * 2, MAX_BATCH_DELAY)
                print(f"  hit quota limits this batch — slowing down to {delay:.1f}s between batches...")
            else:
                delay = max(BASE_BATCH_DELAY, delay * 0.8)
            time.sleep(delay)

    fetch_ids(ids, "scanned")

    retry_round = 1
    while failed_ids and retry_round <= 3:
        print(f"  retrying {len(failed_ids)} message(s) that hit transient/quota errors "
              f"(round {retry_round})...")
        time.sleep(15 * retry_round)
        to_retry, failed_ids = failed_ids, []
        fetch_ids(to_retry, f"retry round {retry_round}")
        retry_round += 1

    if failed_ids:
        fetched += len(failed_ids)
        print(f"  warning: {len(failed_ids)} message(s) could not be fetched after retries and were skipped.")

    print(f"Scanned {fetched} messages. Found {len(senders)} senders with an unsubscribe option.")
    return senders


def do_https_unsubscribe(url, one_click):
    headers = {"User-Agent": USER_AGENT}
    if one_click:
        resp = requests.post(
            url, headers=headers,
            data={"List-Unsubscribe": "One-Click"},
            timeout=REQUEST_TIMEOUT,
        )
    else:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.status_code


def do_mailto_unsubscribe(service, address):
    message = MIMEText("")
    message["to"] = address
    message["subject"] = "unsubscribe"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    execute_with_retry(service.users().messages().send(userId="me", body={"raw": raw}))


def perform_unsubscribe(service, addr, entry, log):
    """Actually send the unsubscribe request/email. Updates and saves log. Returns success bool."""
    method = method_label(entry)
    try:
        if entry["https"]:
            status = do_https_unsubscribe(entry["https"], entry["one_click"])
            print(f"  Sent request to unsubscribe link (HTTP {status}).")
        elif entry["mailto"]:
            do_mailto_unsubscribe(service, entry["mailto"])
            print("  Sent unsubscribe email.")
        else:
            print("  No usable link found, skipping.")
            return False
        log[addr] = {"status": "done", "at": datetime.now(timezone.utc).isoformat(), "method": method}
        save_log(log)
        return True
    except Exception as e:
        print(f"  Failed: {e}")
        log[addr] = {"status": "failed", "at": datetime.now(timezone.utc).isoformat(), "error": str(e)}
        save_log(log)
        return False


def list_message_ids(service, query, cap=5000):
    ids = []
    request = service.users().messages().list(userId="me", q=query, maxResults=500)
    while request is not None and len(ids) < cap:
        response = execute_with_retry(request)
        ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return ids


def trash_messages(service, message_ids):
    """Move messages to Gmail Trash (recoverable for 30 days) — never permanently deletes."""
    trashed = 0
    for i in range(0, len(message_ids), TRASH_CHUNK_SIZE):
        chunk = message_ids[i:i + TRASH_CHUNK_SIZE]
        execute_with_retry(service.users().messages().batchModify(
            userId="me", body={"ids": chunk, "addLabelIds": ["TRASH"]}
        ))
        trashed += len(chunk)
    return trashed


def offer_trash_cleanup(service, addr, log, dry_run, auto=False):
    """After a successful unsubscribe, offer to move that sender's existing emails to Trash."""
    ids = list_message_ids(service, f"from:{addr}")
    if not ids:
        return
    if not auto:
        choice = input(
            f"  Also move their {len(ids)} existing email(s) to Trash (recoverable for 30 days)? [y/n]: "
        ).strip().lower()
        if choice not in ("y", "yes"):
            return
    if dry_run:
        print(f"  [dry-run] would move {len(ids)} email(s) to Trash.")
        return
    trashed = trash_messages(service, ids)
    print(f"  Moved {trashed} email(s) to Trash.")
    entry = log.setdefault(addr, {})
    entry["cleaned_at"] = datetime.now(timezone.utc).isoformat()
    entry["trashed_count"] = trashed
    save_log(log)


def replay_dry_run_approvals(service, approved, log, dry_run, auto=False):
    print(f"\n{len(approved)} sender(s) you already approved in a previous --dry-run:")
    for addr, entry in approved:
        print(f"  - {entry['from_header'] or addr} ({entry['count']} emails)")
    review_each = False
    if not auto:
        choice = input(
            f"Unsubscribe from all {len(approved)} now? [y]es all / [r]eview one-by-one / [n]o skip all: "
        ).strip().lower()
        if choice in ("n", "no", ""):
            return
        review_each = choice in ("r", "review")
    for addr, entry in approved:
        if review_each:
            c = input(f"  Unsubscribe {entry['from_header'] or addr}? [y/n]: ").strip().lower()
            if c not in ("y", "yes"):
                continue
        print(f"  Unsubscribing {addr}...")
        if dry_run:
            print("  [dry-run] would unsubscribe now.")
            continue
        if perform_unsubscribe(service, addr, entry, log):
            offer_trash_cleanup(service, addr, log, dry_run, auto=auto)


def interactive_review(service, senders, log, dry_run, auto=False):
    items = sorted(senders.items(), key=lambda kv: -kv[1]["count"])

    approved_replay = [
        (addr, e) for addr, e in items
        if log.get(addr, {}).get("status") == "dry-run-approved"
    ]
    to_review = [
        (addr, e) for addr, e in items
        if log.get(addr, {}).get("status") not in ("done", "skipped-permanent", "dry-run-approved")
    ]

    if approved_replay:
        if dry_run:
            print(f"\n{len(approved_replay)} sender(s) already approved in a previous dry-run "
                  "(re-run without --dry-run to actually unsubscribe).")
        else:
            replay_dry_run_approvals(service, approved_replay, log, dry_run, auto=auto)

    if not to_review:
        if not approved_replay:
            print("Nothing new to review (everything already handled — see unsubscribe_log.json).")
        return

    if auto:
        print(f"\n{len(to_review)} sender(s) — auto-unsubscribing from all (--yes).\n")
    else:
        print(f"\n{len(to_review)} sender(s) to review.\n")

    for i, (addr, entry) in enumerate(to_review, 1):
        print("-" * 60)
        print(f"[{i}/{len(to_review)}] {entry['from_header'] or addr}")
        print(f"  Address:  {addr}")
        print(f"  Emails:   {entry['count']}")
        for s in entry["subjects"]:
            print(f"  Subject:  {s}")
        method = method_label(entry)
        print(f"  Method:   {method}")
        if entry["https"]:
            print(f"  Link:     {entry['https']}")
        if entry["mailto"]:
            print(f"  Mailto:   {entry['mailto']}")

        if auto:
            choice = "y"
        else:
            choice = input("  Unsubscribe? [y]es / [n]o skip / [never] don't ask again / [q]uit: ").strip().lower()
        if choice in ("q", "quit"):
            print("Stopping.")
            break
        if choice in ("never",):
            log[addr] = {"status": "skipped-permanent", "at": datetime.now(timezone.utc).isoformat()}
            save_log(log)
            continue
        if choice not in ("y", "yes"):
            continue  # skip for now, ask again next run

        if dry_run:
            log[addr] = {"status": "dry-run-approved", "at": datetime.now(timezone.utc).isoformat()}
            save_log(log)
            print("  [dry-run] would unsubscribe now. Re-run without --dry-run to actually do it.")
            continue

        if perform_unsubscribe(service, addr, entry, log):
            offer_trash_cleanup(service, addr, log, dry_run, auto=auto)


def cleanup_mode(service, log, dry_run, auto=False):
    """Go back through senders already processed (unsubscribed or not) and offer to
    trash their old mail — a failed unsubscribe attempt shouldn't block cleanup of
    mail already sitting in the inbox from that sender."""
    candidates = [addr for addr, info in log.items()
                  if info.get("status") in ("done", "failed") and "cleaned_at" not in info]
    if not candidates:
        print("Nothing to clean up — every processed sender has already been reviewed for cleanup.")
        return
    print(f"{len(candidates)} sender(s) not yet cleaned up.\n")
    for addr in candidates:
        print("-" * 60)
        print(f"Sender: {addr}")
        offer_trash_cleanup(service, addr, log, dry_run, auto=auto)


def trash_query_mode(service, query, dry_run, auto):
    """Move every message matching a Gmail query straight to Trash — no unsubscribe
    attempt, for cleaning up senders with no usable List-Unsubscribe header."""
    ids = list_message_ids(service, query, cap=20000)
    if not ids:
        print(f"No messages match {query!r}.")
        return
    print(f"{len(ids)} message(s) match {query!r}.")
    if not auto:
        choice = input(
            f"Move all {len(ids)} to Trash (recoverable for 30 days, not permanently deleted)? [y/n]: "
        ).strip().lower()
        if choice not in ("y", "yes"):
            print("Aborted.")
            return
    if dry_run:
        print(f"[dry-run] would move {len(ids)} email(s) to Trash.")
        return
    trashed = trash_messages(service, ids)
    print(f"Moved {trashed} email(s) to Trash.")


def verify_mode(service, log):
    """Check whether senders marked 'done' actually stopped emailing."""
    now = datetime.now(timezone.utc)
    checked = 0
    for addr, info in sorted(log.items()):
        if info.get("status") != "done":
            continue
        at = info.get("at")
        if not at:
            continue
        unsub_date = datetime.fromisoformat(at)
        days_since = (now - unsub_date).days
        if days_since < 3:
            print(f"? {addr}: unsubscribed {days_since}d ago, too soon to tell (senders can take up to "
                  "~10 business days to honor requests)")
            continue
        checked += 1
        date_str = unsub_date.strftime("%Y/%m/%d")
        resp = execute_with_retry(service.users().messages().list(
            userId="me", q=f"from:{addr} after:{date_str}", maxResults=5
        ))
        count = len(resp.get("messages", []))
        if count > 0:
            print(f"⚠ {addr}: still received mail {days_since}d after unsubscribing — may not have worked")
        else:
            print(f"✓ {addr}: no new mail {days_since}d after unsubscribing")
    if checked == 0:
        print("No unsubscribed senders are old enough to verify yet.")


def main():
    parser = argparse.ArgumentParser(description="Detect and unsubscribe from marketing emails in Gmail.")
    parser.add_argument("--query", default="category:promotions",
                         help="Gmail search query to scan (default: category:promotions)")
    parser.add_argument("--limit", type=int, default=300, help="Max messages to scan (default 300)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, don't send anything")
    parser.add_argument("--rescan", action="store_true", help="Ignore the log and review everything again")
    parser.add_argument("--cleanup", action="store_true",
                         help="Skip scanning; offer to Trash old mail from already-unsubscribed senders")
    parser.add_argument("--verify", action="store_true",
                         help="Skip scanning; check whether unsubscribed senders actually stopped emailing")
    parser.add_argument("--yes", action="store_true",
                         help="Don't ask per sender: auto-unsubscribe and move existing mail to Trash for "
                              "every sender found (still asks one upfront confirmation)")
    parser.add_argument("--trash-query", metavar="QUERY",
                         help="Skip unsubscribe entirely; just move every message matching this Gmail "
                              "search query straight to Trash (e.g. --trash-query 'category:promotions')")
    args = parser.parse_args()

    service = get_service()
    log = {} if args.rescan else load_log()

    if args.trash_query:
        trash_query_mode(service, args.trash_query, args.dry_run, args.yes)
        return

    if args.verify:
        verify_mode(service, log)
        return

    if args.yes and not args.dry_run:
        scope = "already-unsubscribed senders' old mail" if args.cleanup else \
            f"every sender found (scanning up to {args.limit} messages)"
        choice = input(
            f"--yes: this will auto-unsubscribe and move existing mail to Trash (recoverable for 30 "
            f"days, not permanently deleted) for {scope}, with no per-sender prompts. Proceed? [y/n]: "
        ).strip().lower()
        if choice not in ("y", "yes"):
            print("Aborted.")
            return

    if args.cleanup:
        cleanup_mode(service, log, args.dry_run, auto=args.yes)
        return

    senders = scan(service, args.query, args.limit)
    interactive_review(service, senders, log, args.dry_run, auto=args.yes)


if __name__ == "__main__":
    main()
