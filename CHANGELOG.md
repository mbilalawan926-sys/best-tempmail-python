# Changelog

## 2.0.0

A major version because two defaults changed. If you only ever call
`wait_for_message` after triggering a signup, which is the common case, the new
behaviour is what you wanted all along and nothing else needs touching.

### Breaking

**`wait_for_message` now looks at mail that has already been delivered.**

The API's long-poll endpoint signals mail *arriving*, so a message that landed
before the call started was invisible to it. In automated testing that is the
normal case rather than the edge case: you create the inbox, trigger the
signup, and the email frequently wins the race against your next line of code.
1.x then burned the whole timeout and returned `None` while the message sat
unread in the inbox.

This is not hypothetical. The GitHub Action for this same API shipped with 21
passing tests and failed on the first real email for exactly this reason,
because its mock answered the long poll whenever asked and so could not express
the difference.

Pass `include_existing=False` for the old behaviour, which is the right choice
when you are watching one inbox for successive messages.

**`timeout` is honoured in full rather than clamped to one request.**

1.x did `min(timeout, 55)` and made a single call, so `timeout=120` quietly
waited 55 seconds and reported no mail, with nothing telling the caller it had
given up early. It now loops until the deadline, asking the server only for
what the server will give.

**A filter switches the strategy.** `subject`, `from_` or `q` makes the wait
poll the message listing instead of long-polling, because the long poll cannot
filter and would return the first message to arrive whether or not it is the
one being waited for.

### Added

- `wait_for_code(address, ...) -> str`. The one-liner most tests want. Raises
  when the message holds no recognisable code, because a caller asking for a
  bare string has nowhere to put that outcome and would otherwise type an empty
  string into the form under test.
- `get_links`, `wait_for_links`, `wait_for_action_url`. Link extraction, the
  counterpart to OTP extraction for emails that want a click. Links are typed,
  so unsubscribe, social, legal, tracking and view-in-browser links are
  labelled instead of competing to be the answer, and `Link.destination`
  unwraps click trackers.
- `get_usage() -> Usage`. Plan, quota and subscription. Informative without an
  API key, where it reports the free tier including how many of today's three
  inboxes the current IP has used.
- `create_inboxes(count, ...) -> BulkResult`. Up to 50 inboxes in one call, for
  a suite that runs in parallel. `quota_charged` reports the cost rather than
  leaving you to work it out.
- `list_messages(...) -> MessageList`, carrying `has_more`, `scanned` and
  `scan_limit_reached`. Those last two are what make an empty result
  interpretable: empty with `scan_limit_reached` false means nothing matches,
  definitively.
- Search and filter arguments on both listing methods: `offset`, `from_`,
  `subject`, `q`, `since`, `until`, `has_attachments`. Previously only `limit`
  was reachable, so none of the server's filtering could be used at all.
- `create_inbox(lifetime_minutes=...)`, with `Inbox.lifetime_minutes` and
  `Inbox.note`. Plan-capped inbox lifetimes were unreachable from Python
  before. Asking for more than the plan allows does not fail: the inbox is
  created at the maximum and `note` is the only warning that it expires sooner
  than asked.

### Fixed

- `delete_inbox` no longer raises on an inbox that has already expired or been
  deleted; it returns `False`. Cleanup usually runs after the thing it is
  cleaning up may be gone, and anything that turns a passing test red for
  tidying up after itself is a bad trade. `missing_ok=False` restores the old
  behaviour.
- Addresses and message ids are percent-encoded in URLs. A plus-addressed
  inbox was previously interpolated raw.

### Verified

The 1.0.0 test suite still passes in full, 28 of 28, with one assertion
rewritten: it asserted that `wait_for_message` returned `None` while a message
sat in the inbox, which is the bug this release fixes. 31 new tests cover the
additions, and two of them use a mock that refuses to answer the long poll at
all. Those two were checked to fail when the fix is removed, because a
regression test that passes either way is decoration.

## 1.0.0

First release.
