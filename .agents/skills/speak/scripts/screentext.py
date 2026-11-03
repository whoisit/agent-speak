"""Find where a spoken sentence is shown on screen, in a terminal window, through UI Automation.

Windows Terminal (and the classic console) expose their visible text through UI Automation, the
interface screen readers use. Its text comes back as one line per row, which is also how it is
navigated, so a match found in the text is turned back into screen rectangles row by row and
column by column.

What is spoken isn't exactly what is shown: markdown is gone, paths are cut to file names, tables
are skipped, and the terminal wraps lines with indentation. So the chunk being read is matched by
its words, allowing for some missing or extra ones (a local alignment, as in diff tools). When the
same words appear more than once on screen, the lowest wins, since the reply being read is
normally the newest output.

Every call here blocks on the terminal, so this runs on a worker thread of its own.
"""
import re
import sys
import time

WORD = re.compile(r"\w+")
MATCH, MISMATCH, GAP = 2, -1, -1   # alignment scores
MIN_MATCHED = 0.5                  # share of a chunk's words that must be found on screen
ELEMENTS_EVERY = 2.0               # seconds between re-finding a window's text controls


def words(text):
    return [m.group().lower() for m in WORD.finditer(text)]


def align(needle, hay):
    """Best local alignment of the word list `needle` within `hay`: [(needle index, hay index)] of the
    words matched, or [] if too few match. Ties go to the latest position in `hay`."""
    m, n = len(needle), len(hay)
    if not m or not n:
        return []
    score = [[0] * (n + 1) for _ in range(m + 1)]
    best, best_at = 0, None
    for i in range(1, m + 1):
        word, prev, row = needle[i - 1], score[i - 1], score[i]
        for j in range(1, n + 1):
            s = max(0, prev[j - 1] + (MATCH if hay[j - 1] == word else MISMATCH), prev[j] + GAP, row[j - 1] + GAP)
            row[j] = s
            if s and (s > best or s == best and j > best_at[1]):
                best, best_at = s, (i, j)
    pairs = []
    i, j = best_at or (0, 0)
    while i and j and score[i][j]:
        if needle[i - 1] == hay[j - 1] and score[i][j] == score[i - 1][j - 1] + MATCH:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif score[i][j] == score[i - 1][j] + GAP:
            i -= 1
        elif score[i][j] == score[i][j - 1] + GAP:
            j -= 1
        else:
            i, j = i - 1, j - 1
    pairs.reverse()
    return pairs if len(pairs) >= max(1, min(m, 2), MIN_MATCHED * m) else []


class ScreenText:
    def __init__(self):
        sys.coinit_flags = 0  # COINIT_MULTITHREADED for this (worker) thread, as UI Automation prefers
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        self.U = U
        self.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        self.has_text = self.uia.CreatePropertyCondition(U.UIA_IsTextPatternAvailablePropertyId, True)
        self.elements, self.found_at = (None, []), 0.0
        self.alignments = {}

    def find(self, hwnd, chunk, first, last):
        """Screen rectangles (left, top, right, bottom), one per row, of the words first..last
        (indexes into words(chunk)) as shown in the window; [] if they can't be found."""
        needle = words(chunk)
        if last < first:
            return []
        best = None
        for rng, lines, hay, places in self._visible(hwnd):
            pairs = self._align(needle, hay)
            if pairs and (best is None or len(pairs) > len(best[0])):
                best = pairs, rng, lines, places
        if not best:
            return []
        pairs, rng, lines, places = best
        shown = [j for i, j in pairs if first <= i <= last]
        if len(shown) < (last - first + 1) / 2:  # most of this sentence isn't on screen
            return []
        (row0, col0, _), (row1, _, col1) = places[shown[0]], places[shown[-1]]
        rects = []
        for row in range(row0, row1 + 1):
            line = lines[row]
            start = col0 if row == row0 else len(line) - len(line.lstrip())
            end = col1 if row == row1 else len(line.rstrip())
            if start < end:
                rects += self._row_rects(rng, row, start, end)
        return rects

    def _visible(self, hwnd):
        """(range, lines, words, word places) for each visible text range in the window, where a
        word's place is (row, start column, end column)."""
        result = []
        for element in self._text_elements(hwnd):
            try:
                pattern = element.GetCurrentPattern(self.U.UIA_TextPatternId).QueryInterface(
                    self.U.IUIAutomationTextPattern)
                ranges = pattern.GetVisibleRanges()
                for k in range(ranges.Length):
                    rng = ranges.GetElement(k)
                    lines = rng.GetText(-1).split("\r\n")
                    hay, places = [], []
                    for row, line in enumerate(lines):
                        for m in WORD.finditer(line):
                            hay.append(m.group().lower())
                            places.append((row, m.start(), m.end()))
                    result.append((rng, lines, hay, places))
            except Exception:  # the control went away, e.g. its tab was closed
                self.elements = (None, [])
        return result

    def _text_elements(self, hwnd):
        """The window's controls that offer their text (one per Windows Terminal pane), cached briefly."""
        now = time.monotonic()
        if self.elements[0] != hwnd or now - self.found_at > ELEMENTS_EVERY:
            found = self.uia.ElementFromHandle(hwnd).FindAll(self.U.TreeScope_Subtree, self.has_text)
            self.elements = hwnd, [found.GetElement(k) for k in range(found.Length)]
            self.found_at = now
        return self.elements[1]

    def _align(self, needle, hay):
        """align(), remembered for the last few screens: most polls see the same text again."""
        key = (tuple(needle), tuple(hay))
        if key not in self.alignments:
            if len(self.alignments) > 8:
                self.alignments.clear()
            self.alignments[key] = align(needle, hay)
        return self.alignments[key]

    def _row_rects(self, rng, row, start, end):
        """Screen rectangles of columns start..end on a row of the visible range."""
        U = self.U
        S, E = U.TextPatternRangeEndpoint_Start, U.TextPatternRangeEndpoint_End
        r = rng.Clone()
        r.MoveEndpointByRange(E, r, S)  # empty, at the top left
        if r.Move(U.TextUnit_Line, row) != row:
            return []
        r.MoveEndpointByUnit(E, U.TextUnit_Character, end)
        r.MoveEndpointByUnit(S, U.TextUnit_Character, start)
        flat = r.GetBoundingRectangles() or ()
        return [(flat[k], flat[k + 1], flat[k] + flat[k + 2], flat[k + 1] + flat[k + 3])
                for k in range(0, len(flat) - 3, 4)]
