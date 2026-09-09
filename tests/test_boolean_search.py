"""Unit tests for boolean_search.

boolean_search imports nothing outside the standard library, so these run on a
fresh clone with no packages installed.
"""
import unittest

import boolean_search as bs

# Non-ASCII test data is written as escapes so this file stays pure ASCII and
# cannot be corrupted by a checkout with the wrong encoding.
G_BREVE = "\u011f"          # small g with breve, as in Erdogan spelled properly
CURLY_APOSTROPHE = "\u2019"  # right single quotation mark, what most feeds emit


def matched(query, title, description=""):
    return bs.evaluate(bs.parse_query(query), title, description)[0]


def score(query, title, description=""):
    return bs.evaluate(bs.parse_query(query), title, description)[1]


class ParsingTests(unittest.TestCase):
    def test_blank_query_has_no_ast(self):
        self.assertIsNone(bs.parse_query(""))
        self.assertIsNone(bs.parse_query("   "))

    def test_operators_alone_are_not_a_query(self):
        # A query of pure operators must not parse into something that
        # trivially matches every item.
        for junk in ("AND OR", "NOT", "()", "-"):
            with self.subTest(junk=junk):
                self.assertFalse(bs.has_real_term(bs.parse_query(junk)))

    def test_real_term_is_detected(self):
        self.assertTrue(bs.has_real_term(bs.parse_query("cat")))
        self.assertTrue(bs.has_real_term(bs.parse_query('"exact phrase"')))
        self.assertTrue(bs.has_real_term(bs.parse_query("cat AND NOT dog")))

    def test_operators_are_case_insensitive(self):
        self.assertTrue(matched("cat or dog", "a dog barks"))
        self.assertTrue(matched("cat OR dog", "a dog barks"))


class MatchingTests(unittest.TestCase):
    def test_bare_terms_imply_and(self):
        self.assertTrue(matched("cat dog", "the cat and the dog"))
        self.assertFalse(matched("cat dog", "the cat alone"))

    def test_explicit_and_or(self):
        self.assertTrue(matched("cat AND dog", "cat and dog together"))
        self.assertFalse(matched("cat AND dog", "cat only"))
        self.assertTrue(matched("cat OR dog", "cat only"))
        self.assertFalse(matched("cat OR dog", "neither animal here"))

    def test_not_excludes(self):
        self.assertTrue(matched("cat NOT dog", "cat and mouse"))
        self.assertFalse(matched("cat NOT dog", "cat and dog"))

    def test_dash_is_shorthand_for_not(self):
        self.assertTrue(matched("cat -dog", "cat and mouse"))
        self.assertFalse(matched("cat -dog", "cat and dog"))

    def test_dash_term_with_trailing_punctuation_still_negates(self):
        # Regression: the negated term used to skip the punctuation scrub that
        # bare terms get, so "-dog," excluded the literal token "dog," which
        # nothing can ever match, quietly turning the NOT into a no-op.
        self.assertFalse(matched("cat -dog,", "cat and dog"))

    def test_parentheses_group(self):
        self.assertTrue(matched("(cat OR dog) AND vet", "the cat visits the vet"))
        self.assertFalse(matched("(cat OR dog) AND vet", "the cat runs away"))

    def test_closing_paren_is_its_own_token(self):
        # Regression: a bare token used to run to the next whitespace, so the
        # ")" in "dog)" was absorbed into the word and then scrubbed away. The
        # group never closed and "(cat OR dog) AND vet" reassociated into
        # "cat OR (dog AND vet)", which matches a very different set of items.
        self.assertEqual(
            bs.parse_query("(cat OR dog) AND vet"),
            ("AND", ("OR", ("WORD", "cat"), ("WORD", "dog")), ("WORD", "vet")),
        )

    def test_quoted_phrase_needs_adjacency(self):
        self.assertTrue(matched('"big cat"', "a big cat appeared"))
        self.assertFalse(matched('"big cat"', "a big black cat appeared"))

    def test_description_is_searched_as_well_as_title(self):
        self.assertTrue(matched("budget", "Unrelated headline", "the budget passed"))


class ScoringTests(unittest.TestCase):
    def test_title_outweighs_description(self):
        self.assertEqual(score("budget", "Budget passes", ""), 3)
        self.assertEqual(score("budget", "Unrelated headline", "the budget passes"), 1)

    def test_phrase_outscores_loose_words(self):
        self.assertEqual(score('"big cat"', "a big cat appeared", ""), 15)
        self.assertEqual(score('"big cat"', "unrelated", "a big cat appeared"), 8)
        self.assertGreater(
            score('"big cat"', "a big cat appeared"),
            score("big cat", "a big cat appeared"),
        )

    def test_and_sums_both_sides_but_only_when_matched(self):
        self.assertEqual(score("cat dog", "cat dog"), 6)
        self.assertEqual(score("cat dog", "cat only"), 0)

    def test_non_match_scores_zero(self):
        self.assertEqual(score("giraffe", "nothing relevant here"), 0)


class StemmingTests(unittest.TestCase):
    def test_plural_matches_singular(self):
        self.assertTrue(matched("rescues", "rescue effort underway"))
        self.assertTrue(matched("rescue", "several rescues reported"))

    def test_past_tense_matches_base(self):
        self.assertTrue(matched("rescued", "a rescue was attempted"))

    def test_ies_plural(self):
        self.assertTrue(matched("stories", "the story of the year"))

    def test_doubled_consonant_before_ing(self):
        self.assertTrue(matched("running", "they run every day"))

    def test_double_s_is_not_treated_as_a_plural(self):
        # "press" must not stem to "pres".
        self.assertEqual(bs._stem_candidates("press"), {"press"})


class DigitAndUnicodeTermTests(unittest.TestCase):
    """Regression tests for terms the old [A-Za-z']+ character class deleted."""

    def test_year_is_part_of_the_query(self):
        self.assertTrue(matched("election 2026", "Election set for 2026"))
        # Previously the "2026" was stripped and this matched on "election" alone.
        self.assertFalse(matched("election 2026", "Election set for 2030"))

    def test_a_bare_number_is_a_valid_query(self):
        ast = bs.parse_query("2026")
        self.assertTrue(bs.has_real_term(ast))
        self.assertTrue(matched("2026", "Budget for 2026 announced"))

    def test_alphanumeric_name(self):
        self.assertTrue(matched("COP29", "COP29 summit opens in Dubai"))
        self.assertFalse(matched("COP29", "COP summit opens in Dubai"))

    def test_non_ascii_name_matches_itself(self):
        name = "Erdo" + G_BREVE + "an"
        self.assertTrue(matched(name, name + " addresses parliament"))

    def test_two_character_terms_are_searchable(self):
        self.assertTrue(matched("EU", "EU leaders meet in Brussels"))
        # ...without becoming a substring match on longer words.
        self.assertFalse(matched("EU", "European leaders meet in Brussels"))

    def test_typographic_apostrophe_folds_to_ascii(self):
        headline = "Ireland" + CURLY_APOSTROPHE + "s economy grows"
        self.assertTrue(matched("Ireland's", headline))


if __name__ == "__main__":
    unittest.main()
