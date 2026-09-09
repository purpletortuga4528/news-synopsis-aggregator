import re
from collections import Counter

STOPWORDS = set(
    """
    a about above after again against all am an and any are aren't as at be
    because been before being below between both but by can't cannot could
    couldn't did didn't do does doesn't doing don't down during each few for
    from further had hadn't has hasn't have haven't having he he'd he'll
    he's her here here's hers herself him himself his how how's i i'd i'll
    i'm i've if in into is isn't it it's its itself let's me more most
    mustn't my myself no nor not of off on once only or other ought our ours
    ourselves out over own same shan't she she'd she'll she's should
    shouldn't so some such than that that's the their theirs them themselves
    then there there's these they they'd they'll they're they've this those
    through to too under until up very was wasn't we we'd we'll we're we've
    were weren't what what's when when's where where's which while who
    who's whom why why's with won't would wouldn't you you'd you'll you're
    you've your yours yourself yourselves
    """.split()
)

# The sentence after a full stop can begin with a capital that is not in A-Z:
# "Elysee" with an acute E, "Uber" with an umlaut. Without the non-ASCII branch
# those paragraphs never split at all and the whole thing is treated as one
# enormous sentence. The branch is deliberately loose (it admits lowercase
# accented letters too), which can over-split after an abbreviation, but that
# failure already exists for ASCII ("M. Dupont") and costs far less than not
# splitting.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"']|[^\x00-\x7F])")

# Unicode-aware, mirroring boolean_search: an [A-Za-z']+ class chops a name
# with a diacritic into fragments and deletes numbers outright, so a shared
# year or a recurring non-English name contributes nothing to clustering.
# [^\W_] is "word character but not underscore". Kept as a local copy rather
# than imported so this module stays standalone stdlib, like boolean_search.
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
_APOSTROPHE_RE = re.compile("[\u2019\u02bc]")  # right single quote, modifier apostrophe


def _normalize(text: str) -> str:
    """Fold typographic apostrophes so "don't" written either way is one word."""
    return _APOSTROPHE_RE.sub("'", text)


# A full stop inside an abbreviation looks exactly like a sentence boundary:
# "Osaka kicked off her 2026 U.S. Open campaign" was being cut after "U.S.",
# leaving a fragment that carries no fact and pollutes both the summary and the
# cross-source comparison. Detected after splitting rather than with lookbehind,
# because Python requires fixed-width lookbehind and these vary in length.
_ABBREVIATIONS = frozenset(
    """
    mr mrs ms dr prof rev fr sr jr st sen rep gov pres gen adm col maj capt lt
    sgt hon vs etc no inc ltd co corp dept est fig al approx apt ave blvd
    jan feb mar apr jun jul aug sept sep oct nov dec mon tues tue wed thurs thu
    fri sat sun
    """.split()
)
# Two or more single-letter-plus-dot runs: U.S., U.K., e.g., i.e., a.m.
_INITIALISM_RE = re.compile(r"(?:[^\W\d_]\.){2,}$")
_TRAILING_WORD_RE = re.compile(r"([^\W\d_]+)\.$")


def _ends_with_abbreviation(fragment: str) -> bool:
    fragment = fragment.rstrip()
    if _INITIALISM_RE.search(fragment):
        return True
    match = _TRAILING_WORD_RE.search(fragment)
    return bool(match) and match.group(1).lower() in _ABBREVIATIONS


# Lines a page carries around its article rather than in it. These reach the
# summarizer as ordinary sentences and were being selected as claims: a synopsis
# asserting "Published 2 hours ago" is worse than one sentence shorter. Kept
# deliberately narrow, anchored at the start, so real prose is not discarded.
_BOILERPLATE_RES = (
    # "Published" and "Updated" only when an actual date or time follows. A bare
    # prefix match would discard real prose such as "Published research shows a
    # decline in numbers".
    re.compile(r"^published[:\s]+(?:on\s+|at\s+)?\d", re.I),
    re.compile(r"^(?:last\s+)?updated[:\s]+(?:on\s+|at\s+)?\d", re.I),
    # Calls to action, as phrases rather than bare words, so a sentence opening
    # "Share prices fell" or "Copyright law changed" is not thrown away.
    re.compile(r"^(?:share this|share on|follow us|sign up for|subscribe to"
               r"|read more|related stories|advertisement|all rights reserved)\b", re.I),
    re.compile(r"^\d+\s*(?:min|mins|minute|minutes|hour|hours)\s+(?:read|ago)\b", re.I),
    re.compile(r"^(?:image|photo|picture|video)\s+(?:source|caption|credit)\b", re.I),
)


def _is_boilerplate(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _BOILERPLATE_RES)


def _split_sentences(text: str):
    """Split into sentences, treating a newline as a hard boundary.

    Newlines arrive from separate HTML block elements. Within a block, ordinary
    sentence punctuation applies; across blocks there is often no punctuation at
    all, because a headline, a byline and the first paragraph are simply
    adjacent. Joining those with a space produced sentences like "...died in the
    crash Published 9 September 2026", which no start-anchored filter can catch
    and which read as nonsense in a synopsis.
    """
    out = []
    for block in _normalize(text).split("\n"):
        block = re.sub(r"\s+", " ", block).strip()
        if not block:
            continue
        merged = []
        for fragment in _SENTENCE_SPLIT_RE.split(block):
            if merged and _ends_with_abbreviation(merged[-1]):
                merged[-1] = merged[-1] + " " + fragment
            else:
                merged.append(fragment)
        out.extend(s.strip() for s in merged if s.strip())
    return out


def keywords(text: str) -> set:
    """Lowercase, stopword-free, len>2 word set. Used both for summarization scoring
    and for cross-source similarity comparison."""
    return {
        w.lower()
        for w in _WORD_RE.findall(_normalize(text))
        if len(w) > 2 and w.lower() not in STOPWORDS
    }


POSITION_WEIGHT = 0.15  # small lede bias: earlier sentences edge out equally-scored later ones
DEDUP_JACCARD_THRESHOLD = 0.6  # skip a candidate this similar to one already picked


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _rank_sentences(text: str, min_words_per_sentence: int = 6):
    """Return (sentence, original_index, score, keyword_set) tuples sorted by score,
    descending. Score blends word-frequency salience with a lede bias (earlier
    sentences in a news article tend to carry the actual news)."""
    sentences = [s for s in _split_sentences(text) if not _is_boilerplate(s)]
    candidates = [s for s in sentences if len(_WORD_RE.findall(s)) >= min_words_per_sentence]
    if not candidates:
        candidates = sentences
    if not candidates:
        return []

    word_freq = Counter()
    for sentence in candidates:
        word_freq.update(keywords(sentence))

    if not word_freq:
        return [(s, i, 0.0, set()) for i, s in enumerate(candidates)]

    max_freq = max(word_freq.values())
    for word in word_freq:
        word_freq[word] /= max_freq

    total = len(candidates)
    scored = []
    for idx, sentence in enumerate(candidates):
        words = keywords(sentence)
        if not words:
            continue
        freq_score = sum(word_freq.get(w, 0) for w in words) / len(words)
        position_bonus = POSITION_WEIGHT * max(0.0, 1 - idx / total)
        scored.append((sentence, idx, freq_score + position_bonus, words))

    return sorted(scored, key=lambda x: x[2], reverse=True)


def top_sentences(text: str, max_sentences: int = 3, min_words_per_sentence: int = 6) -> list:
    """The N most salient sentences, in original reading order. Greedily skips
    candidates too similar to an already-picked sentence, so the summary doesn't
    repeat the same point in slightly different words."""
    ranked = _rank_sentences(text, min_words_per_sentence)

    picked = []
    picked_kw = []
    for sentence, idx, score, kw in ranked:
        if any(_jaccard(kw, other) >= DEDUP_JACCARD_THRESHOLD for other in picked_kw):
            continue
        picked.append((sentence, idx))
        picked_kw.append(kw)
        if len(picked) >= max_sentences:
            break

    picked.sort(key=lambda x: x[1])
    return [s for s, _ in picked]


def summarize(text: str, max_sentences: int = 5, min_words_per_sentence: int = 6) -> str:
    """Frequency-based extractive summary. Pure stdlib, no downloads, no external calls."""
    return " ".join(top_sentences(text, max_sentences, min_words_per_sentence))
