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


def _split_sentences(text: str):
    text = re.sub(r"\s+", " ", _normalize(text)).strip()
    if not text:
        return []
    sentences = _SENTENCE_SPLIT_RE.split(text)
    return [s.strip() for s in sentences if s.strip()]


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
    sentences = _split_sentences(text)
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
