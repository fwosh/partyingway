# Bluesky event reminders

Posts a Bluesky reminder roughly an hour before each event on the
Partyingway Art Parties calendar.

## How it decides what to post

Every 30 minutes it fetches the feed and looks for events starting within
the next 75 minutes that aren't already in `state.json`. In practice a post
lands 45–75 minutes ahead of the event. Widen or narrow with the
`LEAD_MINUTES` env var in the workflow.

Recurring events are expanded client-side, and each occurrence is tracked
separately, a weekly event gets its own post each week, not one ever.

- **Links need facets** or they render as inert text. The script builds them
  for any `http(s)://` URL in the description.
