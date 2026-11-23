PROJECT: A live map of trains in Germany, working name "Delay Radar". It's a public
portfolio project, so code quality, tests, and a good README matter.

KEY FACTS FROM MY SPIKE (prove_feeds.py already works, reuse it):
- No GPS exists. The realtime GTFS-RT feed has trip updates and alerts only,
  zero vehicle positions. Positions are ESTIMATED from schedule + delay.
- The static feed I used (fv_free) is long-distance only, has no shapes.txt,
  and is about 1.7 days old. It matched only 85 of 6,402 trips (at 05:25).
- The realtime protobuf is national: 65,186 trip updates, of which 65,101 are
  NOT in fv_free (regional, S-Bahn, buses). Likely cause: static feed too narrow.
- route_id is empty on realtime trip updates; the route name must come from
  static trips.txt/routes.txt.
- Delay is per stop and changes along the trip. Stops missing from an update
  inherit the previous stop's delay; stops before the first update count as on time.
- Trips can start "yesterday" (times past 24:00), some haven't started yet,
  some have too few stops, and the feed has duplicate trip ids.
- Interpolating by scheduled percentage is wrong. Use real time per stop
  (scheduled + delay) and place the train by where "now" falls between two stops.
- Long legs (e.g. Göttingen to Hannover, 94 km) look wrong as straight lines.
- Environment: Windows, venv at .venv (needs protobuf 7), Python.

RULES FOR YOU: Implement, don't plan. No diagrams or long design essays. Make
reasonable decisions yourself and tell me in one line what you chose. Ask me a
question only if you're truly blocked or a choice is expensive to reverse.
Verify data sources with web search instead of trusting memory. Write tests as
you go. Commit-sized steps, and tell me how to run what you built.