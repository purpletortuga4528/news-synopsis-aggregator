"""Unit tests for summarizer.

Like boolean_search, this module is pure standard library, so these tests need
no installed packages.
"""
import unittest

import summarizer

# Written as escapes so this file stays pure ASCII.
G_BREVE = "\u011f"          # small g with breve
E_ACUTE = "\u00c9"          # capital E with acute, a sentence-initial capital outside A-Z
CURLY_APOSTROPHE = "\u2019"  # right single quotation mark, what most feeds emit


class KeywordTests(unittest.TestCase):
    def test_stopwords_and_short_words_are_dropped(self):
        self.assertEqual(summarizer.keywords("The cat is on a mat"), {"cat", "mat"})

    def test_case_is_folded(self):
        self.assertEqual(summarizer.keywords("Budget BUDGET budget"), {"budget"})

    def test_empty_text(self):
        self.assertEqual(summarizer.keywords(""), set())


class UnicodeTextTests(unittest.TestCase):
    """Regression tests for text the old [A-Za-z']+ class mangled."""

    def test_diacritic_name_stays_one_keyword(self):
        name = "Erdo" + G_BREVE + "an"
        kw = summarizer.keywords(name + " addresses parliament")
        self.assertIn(name.lower(), kw)
        # It used to be chopped at the non-ASCII letter, leaving a stub.
        self.assertNotIn("erdo", kw)

    def test_numbers_are_keywords(self):
        # A shared year is a real clustering signal; it used to be deleted.
        self.assertIn("2026", summarizer.keywords("Budget for 2026 announced"))
        self.assertIn("cop29", summarizer.keywords("COP29 opens in Dubai"))

    def test_typographic_apostrophe_folds_to_ascii(self):
        curly = summarizer.keywords("Ireland" + CURLY_APOSTROPHE + "s economy grows")
        straight = summarizer.keywords("Ireland's economy grows")
        self.assertEqual(curly, straight)

    def test_sentence_starting_with_a_non_ascii_capital_still_splits(self):
        text = "Le president a parle hier soir. " + E_ACUTE + "lysee a confirme la nouvelle."
        # Previously the lookahead only accepted A-Z, so this stayed one sentence.
        self.assertEqual(len(summarizer._split_sentences(text)), 2)

    def test_ascii_sentence_splitting_is_unchanged(self):
        text = "First sentence here. Second sentence here. Third sentence here."
        self.assertEqual(len(summarizer._split_sentences(text)), 3)


class SentenceSelectionTests(unittest.TestCase):
    # Chosen so the highest-scoring sentence is NOT the first one: the second
    # sentence repeats the corpus's most frequent keyword, which outweighs the
    # first sentence's lede bonus. If top_sentences ever returned rank order
    # instead of reading order, this text would expose it.
    REORDERING_TEXT = (
        "A brief note about scheduling appears at the top of this page. "
        "The committee approved the budget, and the committee approved the timeline. "
        "The committee will meet again in spring to review progress."
    )

    NEAR_DUPLICATE_TEXT = (
        "Global markets rallied sharply after the central bank cut interest rates today. "
        "Markets rallied sharply after the central bank cut interest rates. "
        "Separately, heavy rainfall forced dozens of flight cancellations across the region."
    )

    def test_output_is_in_reading_order_not_rank_order(self):
        picked = summarizer.top_sentences(self.REORDERING_TEXT, max_sentences=2)
        self.assertEqual(len(picked), 2)
        positions = [self.REORDERING_TEXT.index(s) for s in picked]
        self.assertEqual(positions, sorted(positions))

    def test_max_sentences_is_respected(self):
        for n in (1, 2, 3):
            with self.subTest(n=n):
                self.assertLessEqual(
                    len(summarizer.top_sentences(self.NEAR_DUPLICATE_TEXT, max_sentences=n)), n
                )

    def test_every_returned_sentence_came_from_the_source(self):
        for sentence in summarizer.top_sentences(self.REORDERING_TEXT, max_sentences=3):
            self.assertIn(sentence, self.REORDERING_TEXT)

    def test_near_duplicates_are_collapsed(self):
        # The first two sentences say the same thing in slightly different words
        # (Jaccard well above the dedup threshold). Asking for two sentences must
        # spend the second slot on the unrelated third sentence, not on a restated
        # version of the first point.
        picked = summarizer.top_sentences(self.NEAR_DUPLICATE_TEXT, max_sentences=2)
        rain = [s for s in picked if "rainfall" in s]
        markets = [s for s in picked if "rallied" in s]
        self.assertEqual(len(markets), 1, "both near-duplicate sentences were kept")
        self.assertEqual(len(rain), 1, "the distinct sentence was crowded out")

    def test_sentences_below_the_word_minimum_are_skipped(self):
        text = "Short one. This is a considerably longer sentence with plenty of words in it."
        picked = summarizer.top_sentences(text, max_sentences=5, min_words_per_sentence=6)
        self.assertEqual(picked, ["This is a considerably longer sentence with plenty of words in it."])

    def test_short_sentences_are_used_when_nothing_else_qualifies(self):
        # Falling back is better than returning an empty summary for a feed item
        # whose description is a single terse line.
        picked = summarizer.top_sentences("Talks collapsed.", max_sentences=3)
        self.assertEqual(picked, ["Talks collapsed."])


class SummarizeTests(unittest.TestCase):
    def test_empty_input_gives_empty_output(self):
        self.assertEqual(summarizer.summarize(""), "")
        self.assertEqual(summarizer.summarize("   \n\t  "), "")

    def test_result_is_a_space_joined_string_of_selected_sentences(self):
        text = SentenceSelectionTests.REORDERING_TEXT
        expected = " ".join(summarizer.top_sentences(text, max_sentences=2))
        self.assertEqual(summarizer.summarize(text, max_sentences=2), expected)

    def test_summary_is_no_longer_than_the_source(self):
        text = SentenceSelectionTests.NEAR_DUPLICATE_TEXT
        self.assertLessEqual(len(summarizer.summarize(text, max_sentences=2)), len(text))


if __name__ == "__main__":
    unittest.main()
