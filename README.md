# News Synopsis Aggregator

A local web app that searches news sources you choose, then builds a single
cross-source report on a topic: what the sources agree on, where they diverge,
and what only one outlet is reporting.

Everything runs on your machine. There is no account, no API key and no model
call. Summarization is extractive: sentences are scored by word frequency and
lifted verbatim from the articles, so nothing in the output is invented.

## How it works

1. You add sources. An RSS or Atom feed URL is detected automatically. Anything
   else is treated as a search page, and you supply the name of its query
   parameter (usually `q` or `s`).
2. You type a topic. Feed items are matched locally with a boolean query
   parser; search pages receive the raw query string and handle it themselves.
3. Matching articles are fetched, stripped to their text, and summarized.
4. Claims from every source are clustered by vocabulary overlap. A claim backed
   by two or more sources becomes a key point; a claim backed by one is listed
   separately rather than presented as consensus.

## Requirements

Python 3.11 or newer. Developed and tested on 3.12.

The floor is set by the pinned dependencies rather than by the code: the
security advisories affecting older `starlette` releases are only fixed in
versions requiring 3.11. Supporting 3.9 would mean shipping known-vulnerable
packages, which is the worse trade.

## Install

```bash
python -m venv venv
```

```bash
venv/Scripts/activate
```

On macOS or Linux the activate step is `source venv/bin/activate`.

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>.

The server refuses any request whose `Host` or `Origin` header does not name
this machine, so binding it to a public interface will not make it reachable.
That is deliberate: the API has no authentication, and a localhost service that
fetches arbitrary URLs is a tempting target for any web page you happen to have
open. Do not expose it.

## Adding sources

Use the UI, or seed a starter set:

```bash
python seed_sources.py
```

The seed script reads `seed_sources.json`, verifies each feed is live before
adding it, and skips sources whose feeds have moved or gone offline. It is safe
to re-run: a source whose name already exists is left alone. Pass `--yes` to
skip the confirmation prompt.

**Read this before seeding.** Both the UI and the seed script ask you to attest
that you have reviewed each outlet's Terms of Service and are satisfied that
automated access for personal search and summarization is permitted. The seeded
list contains official feeds published by each outlet for automated
consumption, but that attestation is yours to make, not the script's. It does
not research anyone's terms for you.

## Search syntax

| Syntax | Meaning |
| --- | --- |
| `climate policy` | Both terms must appear. Bare terms imply AND. |
| `climate OR weather` | Either term. |
| `climate NOT policy` | First term present, second absent. |
| `climate -policy` | Same as NOT. |
| `"carbon capture"` | Exact phrase, scored higher than loose terms. |
| `(climate OR weather) AND policy` | Parentheses group. |

Operators are case insensitive. Terms match with lightweight stemming, so
`rescues` finds `rescue` and `running` finds `run`. Digits and non-ASCII
letters are part of a term, so `COP29`, `election 2026` and names with
diacritics all work. Queries are capped at 200 characters.

Matches in a headline score higher than matches in a summary, and exact phrases
score highest. This syntax applies to RSS sources, which are filtered locally.
Search-page sources get the query string handed to their own search engine
untouched, and it may or may not understand the same operators.

## Reading the report

- **Overview**: a synthesis weighted toward vocabulary that more than one source
  uses. When the sources share no common vocabulary, it says so instead of
  stitching unrelated sentences together and calling it agreement.
- **Key points**: claims corroborated by two or more sources. Each shows every
  source's own sentence rather than one standing in for all of them, because
  matching is lexical and two sentences can share wording while describing
  different events. Seeing both is how you tell corroboration from coincidence.
- **Differences in coverage**: a short summary per source, so divergent framing
  is visible side by side.
- **Reported by one source**: claims that appeared in a single outlet.
- **Sources**: every article the report drew on, newest first.

Sources that were skipped are listed at the bottom with the reason, whether
that is a robots.txt refusal, a network failure, or no matching items.

## Crawling behaviour

- `robots.txt` is honoured for every host, on the initial URL and on every
  redirect hop. An unreachable, oversized or 401/403 `robots.txt` fails closed:
  the crawl does not proceed. The report distinguishes the two cases, because
  they are not the same fact: a source the publisher disallowed is reported as
  disallowed, while one whose `robots.txt` simply could not be read says so
  instead of attributing a refusal nobody made.
- Crawl-delay and request-rate directives are respected, defaulting to 2 seconds
  between requests to the same host when a site specifies nothing.
- Redirects are followed manually, up to 5 hops, so each hop is revalidated.
- URLs resolving to private, loopback, link-local or reserved addresses are
  refused, and the validated address is pinned for the connection so a second
  DNS answer cannot redirect it somewhere internal. TLS is unaffected: the
  certificate, SNI and Host header all still use the original hostname.
- Response bodies are capped at 3 MB of **decoded** data, and abandoned if they
  take longer than 30 seconds. Counting decoded bytes is the point: a limit on
  what is read off the socket bounds the compressed size instead, and a 51 KB
  gzip payload was measured decoding to 52 MB straight through a 3 MB cap.
- `robots.txt` has its own 512 KB cap. It is the first request made to any new
  host, so it is bounded before anything else trusts that host, and an
  oversized or stalled one fails closed exactly like an unreachable one.
- The request timeout is per socket operation, so each body also carries a
  total wall-clock bound. Without it a server trickling one byte per read
  holds a connection open indefinitely without ever tripping the timeout.

Because crawl-delays are enforced per host and honoured serially, a source with
many feeds takes a while on its own. That is the polite behaviour, not a bug.
Two things keep it bounded.

**Caching.** Feeds are cached for 5 minutes and article text for 30 minutes, so
a second search shortly after the first does not re-download anything. This is
where the time goes: a measured six-source search spent 104.9s asleep honouring
crawl-delays against 15.9s of real network I/O. Serving from cache is also the
politer behaviour, because it makes no request at all. Only successful fetches
are cached, so a transient failure does not stick.

**A search budget.** A search stops fetching after 60 seconds and reports what
it has. A source cut short is marked `partial` and still contributes everything
it gathered; one that got nothing at all is marked `timeout`. Both appear in the
status line at the bottom of the report, along with the elapsed time.

## Dependencies

`requirements.txt` pins direct dependencies to versions with no known
advisories, checked with `pip-audit`. Transitive dependencies are left to
resolve, with one exception: `certifi` carries a floor, because it is the CA
bundle every HTTPS verification depends on and pip will leave an outdated one
in place whenever a loose constraint upstream is still satisfied.

That exception is a patch, not a guarantee. Because the other transitive
dependencies float, two installs from the same file can differ, and an
install months from now may resolve to something unaudited. Worth running
before trusting an environment:

```bash
pip-audit -r requirements.txt
```

## Tests

```bash
python -m unittest discover -v
```

The `boolean_search` and `summarizer` tests are pure standard library, so they
run on a fresh clone with nothing installed. `tests/test_crawler_pinning.py`
needs the dependencies and skips cleanly without them. It verifies that the
DNS-rebinding defence actually intercepts sockets rather than only looking as
though it does, and will fail loudly if a urllib3 upgrade changes how the
socket layer resolves its connect function.

## Data

Sources live in `sites.db`, a SQLite file in the project root, created on first
run. It is not tracked by git. Article text is never stored; it is fetched,
summarized and discarded within a single request.

## Known limitations

- **A first, uncached search is slow.** Crawl-delays serialize requests to the
  same host, so a source with a dozen feeds takes tens of seconds on its own.
  Caching and the search budget bound this, but they do not make the first
  search fast. There is no progress reporting while it runs.
- **Search-page scraping is best effort.** It follows up to 3 links from a
  results page and extracts text generically. Sites that render results in
  JavaScript will yield nothing.
- **Summarization is extractive, not abstractive.** It selects sentences; it
  does not write them. Sentences can read as disjointed out of context.
- **Corroborated Key Points are rare, and roughly a third are wrong.** Measured
  over 24 queries against the seeded sources: 8 key points, of which 5 were
  genuine cross-source corroboration and 3 were lexical coincidence. Expect
  about one genuine point per five queries. Two causes, neither fixable by
  tuning: outlets with different editorial focus do not often cover the same
  story, and bag-of-words matching cannot tell "Trump-backed candidate wins
  primary" in one state from the same sentence about another, because the
  disagreement lives in the words the two sentences do not share. This is why
  each key point shows every source's sentence: read them before believing the
  pairing.
- **Stopwords are English only.** Clustering and salience scoring handle text
  in other languages, but common words in those languages are not filtered out,
  so they carry more weight than they should. Two-character terms are also
  excluded from clustering, so `EU` groups nothing even though it is
  searchable.
- **No authentication.** The localhost restriction is the only access control.

## License

MIT. See [LICENSE](LICENSE).
