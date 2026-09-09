"""Boolean query parsing and evaluation for matching RSS feed items against a
search query. Supports AND / OR / NOT (case-insensitive), a leading "-" as
shorthand for NOT, quoted "exact phrases", and parentheses for grouping.
Bare space-separated terms default to AND (all must be present), matching how
most boolean search tools treat implicit combination.

Pure stdlib, no external dependencies.
"""
import re

# A bare token must stop at a parenthesis. With a plain \S+ the closing paren
# of "(cat OR dog) AND vet" is absorbed into the token "dog)" and then scrubbed
# away, so the group never closes and the query silently reassociates into
# "cat OR (dog AND vet)": a different search from the one that was typed.
_TOKEN_RE = re.compile(r'"[^"]*"|[()]|[^\s()]+')

# Unicode-aware word matching. An [A-Za-z']+ class silently deletes every digit
# and every non-ASCII letter, which for news text is not a corner case: it turns
# "election 2026" into a search for "election", "COP29" into "COP", and leaves a
# name spelled with a diacritic unable to match itself. [^\W_] is "word character
# but not underscore", so letters and digits from any script survive.
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
# The same character class, used to scrub punctuation off a single query token.
_QUERY_STRIP_RE = re.compile(r"[^\w']|_")
_APOSTROPHE_RE = re.compile("[\u2019\u02bc]")  # right single quote, modifier apostrophe

# Shortest term worth indexing. Two, not three: news vocabulary is full of
# meaningful two-character terms (EU, UN, US, G7, AI) and a threshold of three
# made every one of them unsearchable.
MIN_TERM_LEN = 2


def _normalize(text: str) -> str:
    """Fold typographic apostrophes to ASCII so a feed's curly "Ireland's"
    matches an "Ireland's" typed with a straight quote."""
    return _APOSTROPHE_RE.sub("'", text)


def _stem_candidates(word: str) -> set:
    """Multiple plausible normalized forms of a word, rather than one canonical
    stem. A single-suffix stemmer maps "rescue"->"rescue" but "rescues"->"rescu",
    which never match each other. Matching on ANY shared candidate sidesteps that."""
    w = word.lower()
    cands = {w}
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        cands.add(w[:-1])  # cats -> cat, rescues -> rescue
    if len(w) > 4 and w.endswith("es"):
        cands.add(w[:-2])  # boxes -> box
    if len(w) > 4 and w.endswith("ies"):
        cands.add(w[:-3] + "y")  # stories -> story
    if len(w) > 4 and w.endswith("ed"):
        cands.add(w[:-2])  # walked -> walk
        cands.add(w[:-1])  # rescued -> rescue
    if len(w) > 5 and w.endswith("ing"):
        stem = w[:-3]
        cands.add(stem)  # walking -> walk
        cands.add(stem + "e")  # making -> make
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            cands.add(stem[:-1])  # running -> runn -> run (doubled consonant before -ing)
    if len(w) > 3 and w.endswith("ly"):
        cands.add(w[:-2])  # quickly -> quick
    return cands


def _text_candidates(text: str) -> set:
    """Flattened union of every word's stem candidates in a piece of text."""
    result = set()
    for w in _WORD_RE.findall(_normalize(text)):
        if len(w) >= MIN_TERM_LEN:
            result |= _stem_candidates(w)
    return result


class ParseError(Exception):
    pass


def _tokenize(query: str) -> list:
    tokens = []
    for raw in _TOKEN_RE.findall(query):
        if raw in ("(", ")"):
            tokens.append((raw, None))
        elif raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            phrase = _normalize(raw[1:-1]).strip().lower()
            if phrase:
                tokens.append(("PHRASE", phrase))
        elif raw.upper() == "AND":
            tokens.append(("AND", None))
        elif raw.upper() == "OR":
            tokens.append(("OR", None))
        elif raw.upper() == "NOT":
            tokens.append(("NOT", None))
        elif raw.startswith("-") and len(raw) > 1 and raw[1].isalnum():
            # Scrub the negated term the same way a bare term is scrubbed;
            # otherwise "-dog," negates the literal token "dog,", and an
            # all-punctuation tail would leave a dangling NOT with no operand.
            word = _QUERY_STRIP_RE.sub("", _normalize(raw[1:])).lower()
            if word:
                tokens.append(("NOT", None))
                tokens.append(("WORD", word))
        else:
            word = _QUERY_STRIP_RE.sub("", _normalize(raw)).lower()
            if word:
                tokens.append(("WORD", word))
    return tokens


class _Parser:
    def __init__(self, tokens: list):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def advance(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse_expr(self):
        node = self.parse_term()
        while self.peek()[0] == "OR":
            self.advance()
            node = ("OR", node, self.parse_term())
        return node

    def parse_term(self):
        node = self.parse_factor()
        while True:
            kind = self.peek()[0]
            if kind == "AND":
                self.advance()
                node = ("AND", node, self.parse_factor())
            elif kind in ("WORD", "PHRASE", "NOT", "("):
                node = ("AND", node, self.parse_factor())  # implicit AND
            else:
                break
        return node

    def parse_factor(self):
        kind, val = self.peek()
        if kind == "NOT":
            self.advance()
            return ("NOT", self.parse_factor())
        if kind == "(":
            self.advance()
            node = self.parse_expr()
            if self.peek()[0] == ")":
                self.advance()
            return node
        if kind == "WORD":
            self.advance()
            return ("WORD", val)
        if kind == "PHRASE":
            self.advance()
            return ("PHRASE", val)
        raise ParseError("Unexpected end of query")


def parse_query(query: str):
    """Returns an AST, or None if the query has no usable terms (e.g. blank,
    or only operators/parentheses)."""
    tokens = _tokenize(query)
    if not tokens:
        return None
    try:
        parser = _Parser(tokens)
        node = parser.parse_expr()
        if parser.i < len(parser.tokens):
            # Trailing junk (e.g. a stray ")"); ignore it rather than failing the search.
            pass
        return node
    except ParseError:
        return None


def evaluate(node, title: str, description: str):
    """Returns (matched: bool, score: int). Title matches are weighted higher
    than description matches; phrase matches score highest of all."""
    if node is None:
        return False, 0
    title_cands = _text_candidates(title)
    desc_cands = _text_candidates(description)
    title_lower = _normalize(title).lower()
    desc_lower = _normalize(description).lower()
    return _eval(node, title_cands, desc_cands, title_lower, desc_lower)


def _eval(node, title_cands: set, desc_cands: set, title_lower: str, desc_lower: str):
    kind = node[0]

    if kind == "WORD":
        term_cands = _stem_candidates(node[1])
        in_title = bool(term_cands & title_cands)
        in_desc = bool(term_cands & desc_cands)
        matched = in_title or in_desc
        score = (3 if in_title else 0) + (1 if in_desc else 0)
        return matched, score

    if kind == "PHRASE":
        phrase = node[1]
        in_title = phrase in title_lower
        in_desc = phrase in desc_lower
        matched = in_title or in_desc
        score = 15 if in_title else (8 if in_desc else 0)
        return matched, score

    if kind == "NOT":
        matched, _ = _eval(node[1], title_cands, desc_cands, title_lower, desc_lower)
        return (not matched), 0

    if kind == "AND":
        lm, ls = _eval(node[1], title_cands, desc_cands, title_lower, desc_lower)
        rm, rs = _eval(node[2], title_cands, desc_cands, title_lower, desc_lower)
        matched = lm and rm
        return matched, (ls + rs if matched else 0)

    if kind == "OR":
        lm, ls = _eval(node[1], title_cands, desc_cands, title_lower, desc_lower)
        rm, rs = _eval(node[2], title_cands, desc_cands, title_lower, desc_lower)
        matched = lm or rm
        score = (ls if lm else 0) + (rs if rm else 0)
        return matched, score

    return False, 0


def has_real_term(node) -> bool:
    """True if the AST contains at least one WORD/PHRASE leaf (guards against
    a query that's only NOT/operators, which would trivially match everything)."""
    if node is None:
        return False
    kind = node[0]
    if kind in ("WORD", "PHRASE"):
        return True
    if kind == "NOT":
        return has_real_term(node[1])
    if kind in ("AND", "OR"):
        return has_real_term(node[1]) or has_real_term(node[2])
    return False
