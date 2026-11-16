"""Key point construction.

Clustering is lexical, so two sentences can match on shape while disagreeing on
the subject. A key point therefore carries each source's own sentence rather than
one representative standing in for all of them, so a reader can see whether the
sources actually agree.
"""
import unittest

try:
    import app
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    DEPS_AVAILABLE = False


def claim(site, sentence, keywords):
    return {"site_name": site, "sentence": sentence, "url": f"https://{site}.test/a", "_kw": set(keywords)}


@unittest.skipUnless(DEPS_AVAILABLE, "requires the third-party dependencies")
class SplitClustersTests(unittest.TestCase):
    def test_two_sources_produce_one_account_each(self):
        cluster = [
            claim("Alpha", "Alpha reported the bridge opened.", {"alpha", "bridge", "opened"}),
            claim("Beta", "Beta reported the bridge opened today.", {"beta", "bridge", "opened", "today"}),
        ]
        key_points, singles = app._split_clusters([cluster])
        self.assertEqual(singles, [])
        self.assertEqual(len(key_points), 1)
        accounts = key_points[0]["accounts"]
        self.assertEqual([a["site_name"] for a in accounts], ["Alpha", "Beta"])
        self.assertEqual(accounts[0]["sentence"], "Alpha reported the bridge opened.")
        self.assertEqual(accounts[1]["sentence"], "Beta reported the bridge opened today.")

    def test_a_mismatched_pair_is_not_presented_as_corroboration(self):
        # The measured false positive: same phrasing, different races. These
        # cluster because they share almost every word; what separates them
        # lives in the words they do not share, which set overlap discards.
        cluster = [
            claim("Fox", "Trump-backed candidate wins primary for New Hampshire district.",
                  {"trump", "backed", "candidate", "wins", "primary", "hampshire"}),
            claim("Politico", "Trump-backed candidate wins primary for Oklahoma governor.",
                  {"trump", "backed", "candidate", "wins", "primary", "oklahoma"}),
        ]
        key_points, singles = app._split_clusters([cluster])
        self.assertEqual(key_points, [], "two different races asserted as one agreed fact")
        self.assertEqual(len(singles), 1, "the claim should still appear, just not as corroboration")

    def test_accounts_naming_the_same_specifics_are_kept(self):
        # Measured genuine pair: the same statement, worded differently.
        cluster = [
            claim("BBC", 'A Reform UK spokesman said the party "denies any wrongdoing".',
                  {"reform", "spokesman", "party", "denies", "wrongdoing"}),
            claim("The Guardian", 'A Reform UK spokesperson said the party "denies any wrongdoing".',
                  {"reform", "spokesperson", "party", "denies", "wrongdoing"}),
        ]
        key_points, _ = app._split_clusters([cluster])
        self.assertEqual(len(key_points), 1)
        self.assertEqual(len(key_points[0]["accounts"]), 2)

    def test_one_source_saying_more_than_another_is_tolerated(self):
        # A real pair where one headline carries detail the other omits. The
        # tolerance exists so that extra detail is not read as disagreement.
        cluster = [
            claim("BBC", "The UK economy grew at its fastest rate for more than two years.",
                  {"economy", "grew", "fastest", "rate", "years"}),
            claim("The Guardian", "AI helps UK economy grow at fastest pace for more than two years",
                  {"helps", "economy", "grow", "fastest", "pace", "years"}),
        ]
        key_points, _ = app._split_clusters([cluster])
        self.assertEqual(len(key_points), 1)

    def test_a_source_with_several_claims_contributes_its_richest(self):
        cluster = [
            claim("Alpha", "Short one.", {"short"}),
            claim("Alpha", "A much fuller account of what happened here.", {"much", "fuller", "account", "happened"}),
            claim("Beta", "Beta covered the same thing.", {"beta", "covered", "same", "thing"}),
        ]
        key_points, _ = app._split_clusters([cluster])
        alpha = [a for a in key_points[0]["accounts"] if a["site_name"] == "Alpha"][0]
        self.assertEqual(alpha["sentence"], "A much fuller account of what happened here.")

    def test_one_account_per_source_not_per_claim(self):
        cluster = [
            claim("Alpha", "First alpha sentence here.", {"first", "alpha"}),
            claim("Alpha", "Second alpha sentence here.", {"second", "alpha"}),
            claim("Beta", "A beta sentence here.", {"beta"}),
        ]
        key_points, _ = app._split_clusters([cluster])
        self.assertEqual(len(key_points[0]["accounts"]), 2)

    def test_single_source_clusters_are_not_key_points(self):
        cluster = [claim("Alpha", "Only Alpha said this.", {"only", "alpha", "said"})]
        key_points, singles = app._split_clusters([cluster])
        self.assertEqual(key_points, [])
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0]["site_name"], "Alpha")
        self.assertNotIn("accounts", singles[0])

    def test_most_corroborated_points_come_first(self):
        two = [claim("A", "x y z", {"x", "y"}), claim("B", "x y z", {"x", "y"})]
        three = [claim("A", "p q", {"p"}), claim("B", "p q", {"p"}), claim("C", "p q", {"p"})]
        key_points, _ = app._split_clusters([two, three])
        self.assertEqual([len(kp["sources"]) for kp in key_points], [3, 2])

    def test_limits_are_applied(self):
        many = [[claim("A", f"s{i}", {f"k{i}"}), claim("B", f"s{i}", {f"k{i}"})] for i in range(20)]
        singles = [[claim("A", f"t{i}", {f"m{i}"})] for i in range(20)]
        key_points, only_one = app._split_clusters(many + singles)
        self.assertEqual(len(key_points), app.MAX_KEY_POINTS)
        self.assertEqual(len(only_one), app.MAX_ONLY_ONE_SOURCE)


if __name__ == "__main__":
    unittest.main()
