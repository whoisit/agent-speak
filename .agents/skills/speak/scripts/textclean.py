"""Turns markdown-ish text into speakable paragraphs (shared by the Stop hook and the clipboard reader)."""
import re


def clean(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)                # code blocks
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("|"))  # tables
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)            # links -> link text
    text = re.sub(r"https?://\S+", " ", text)                         # bare URLs

    def basename(m):
        return re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1]
    path_re = r"[A-Za-z]:\\[^\s`]+|(?<![\w/])[~.]?/?[\w.-]+(?:/[\w.-]+)+/?"
    # Inline code: paths become their file name, long snippets are dropped
    text = re.sub(r"`([^`]*)`", lambda m: "" if len(m.group(1)) > 40 and not re.fullmatch(path_re, m.group(1)) else m.group(1), text)
    text = re.sub(path_re, basename, text)
    text = re.sub(r"(?m)^\s*#+\s*", "", text)                          # headers
    text = re.sub(r"(?m)^\s*([-*_=])(\s*\1){2,}\s*$", "", text)        # horizontal rules
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", text)  # emoji and symbols
    text = re.sub(r"(?m)^\s*([-*+]|\d+\.)\s+", "\n", text)             # list items become paragraphs
    text = re.sub(r"(\*\*|__|\*|~~)(.+?)\1", r"\2", text)              # bold / italic
    text = re.sub(r"[→←↔]", ",", text)
    # Dash ranges read as "to", other en/em dashes as a pause
    text = re.sub(r"(\d)\s*[–—]\s*(?=\d)", r"\1 to ", text)
    text = re.sub(r"\s*[–—]\s*", ", ", text)
    # Paragraphs are kept, separated by a blank line, so the player can skip one at a time
    paragraphs = (re.sub(r"\s+", " ", p) for p in re.split(r"\n\s*\n", text))
    paragraphs = (re.sub(r"\s+([.,;:!?])(?=\s|$)", r"\1", p).strip() for p in paragraphs)
    return "\n\n".join(p for p in paragraphs if p)
