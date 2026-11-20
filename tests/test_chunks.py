"""How replies are cut into chunks for synthesis.  venv\\Scripts\\python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "speak", "scripts"))
import speak  # noqa: E402

REPLY = """I found why it sounds choppy. The cleaner turns every bullet into its own paragraph, and the \
splitter never joins paragraphs, so most chunks end up being a single sentence.

What I changed:

Short paragraphs and bullets are now grouped

The first chunk is still one sentence, so the voice starts quickly

---

Each join gets a pause that matches it.

Tests pass. Do you want me to release it to the public repo, or would you rather listen to it for a \
day first? """ + " ".join(f"This is filler sentence number {n} of a long closing paragraph." for n in range(20))


def last_para(chunk):
    return chunk[0] + chunk[1].count("\n")


class SplitChunks(unittest.TestCase):
    def setUp(self):
        self.paras = speak.paragraphs(REPLY)
        self.chunks = speak.split_chunks(self.paras)

    def test_unspeakable_paragraphs_dropped(self):
        self.assertNotIn("---", self.paras)

    def test_text_kept_in_order(self):
        # Rejoining the chunks gives back the paragraphs
        rebuilt = " ".join(text for _, text, _ in self.chunks).replace(" \n", "\n")
        rebuilt_paras = []
        for p, text, _ in self.chunks:
            for k, line in enumerate(text.split("\n")):
                if k == 0 and rebuilt_paras and p == len(rebuilt_paras) - 1:
                    rebuilt_paras[-1] += " " + line  # carries on the paragraph the last chunk ended in
                else:
                    rebuilt_paras.append(line)
        self.assertEqual(rebuilt_paras, self.paras, rebuilt)

    def test_first_chunk_is_first_sentence(self):
        self.assertEqual(self.chunks[0][1], "I found why it sounds choppy.")

    def test_short_paragraphs_share_a_chunk(self):
        merged = [text for _, text, _ in self.chunks if text.startswith("What I changed:")]
        self.assertEqual(merged[0].count("\n"), 3)  # the heading, both bullets and the next paragraph

    def test_sizes_grow_and_stay_capped(self):
        for prev, cur in zip(self.chunks, self.chunks[1:]):
            limit = min(speak.CHUNK_CHARS, max(speak.MIN_GROWN, speak.CHUNK_GROWTH * len(prev[1])))
            if len(cur[1]) > limit:  # only a sentence too long on its own, which is never cut
                self.assertNotRegex(cur[1], r"[.!?]\s|\n", cur)
        self.assertTrue(any(len(text) > 300 for _, text, _ in self.chunks))

    def test_ends_inside_a_paragraph_only_if_it_began_there(self):
        for cur, nxt in zip(self.chunks, self.chunks[1:]):
            if nxt[0] == last_para(cur):  # cur ended inside a paragraph
                self.assertNotIn("\n", cur[1], cur)

    def test_pauses(self):
        for cur, nxt in zip(self.chunks, self.chunks[1:]):
            kind = "paragraph" if nxt[0] != last_para(cur) else "sentence"
            self.assertEqual(cur[2], speak.PAUSES[kind], cur)
        self.assertEqual(self.chunks[-1][2], speak.PAUSES["paragraph"])

    def test_long_first_sentence_split_at_comma(self):
        text = "When the first sentence of a reply is long, it is cut at a comma so the voice can start " \
               "early, and the rest follows straight after."
        chunks = speak.split_chunks(speak.paragraphs(text))
        self.assertTrue(chunks[0][1].endswith(","))
        self.assertEqual(chunks[0][2], speak.PAUSES["clause"])

    def test_empty(self):
        self.assertEqual(speak.split_chunks(speak.paragraphs("---\n\n🙂")), [])


class SkipParagraph(unittest.TestCase):
    def test_moves_to_a_chunk_that_starts_a_paragraph(self):
        chunks = [(0, "One.", 0), (0, "Two.\nThree.", 0), (2, "Four, long.", 0), (2, "Five.", 0), (3, "Six.", 0)]
        self.assertEqual(speak.next_paragraph(chunks, 0), 2)  # chunk 1 carries on paragraph 0
        self.assertEqual(speak.next_paragraph(chunks, 1), 2)
        self.assertEqual(speak.next_paragraph(chunks, 2), 4)
        self.assertEqual(speak.next_paragraph(chunks, 4), 5)


class Speakable(unittest.TestCase):
    def test_lines_get_a_full_stop(self):
        self.assertEqual(speak.speakable("What I changed:\nGrouped bullets\nDone!"),
                         "What I changed: Grouped bullets. Done!")


if __name__ == "__main__":
    unittest.main()
