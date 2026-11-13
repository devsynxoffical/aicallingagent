"""Find the passages of a campaign's training documents that answer a question.

Ranking is BM25 over normalized words: no external service, no key, a few
milliseconds for a campaign's worth of documents. Words are normalized so that
accents (French), diacritics and letter variants (Arabic) and plurals do not hide a
match. The agent writes its query in the language of the documents, so a question
asked in Arabic still finds an English price sheet.

An index is always built for one organization and one campaign. There is no way to
ask it for anything else.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import KnowledgeChunk, TrainingDocument

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ARABIC_MARKS = re.compile(r"[ً-ٰٟـ]")  # diacritics and tatweel
_ARABIC_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
# Words too common to tell passages apart; BM25 already discounts them, dropping them keeps queries sharp.
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from has have how i in is it of on or our that the this to we what "
    "when where which who will with you your can de des du et la le les un une est que qui pour dans vous nous".split()
)


def _stem(token: str) -> str:
    """Crude English/French suffix stripping so price, prices and pricing meet, as do
    post, posted and posting. Over-stemming costs nothing here: it only widens recall."""
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 4 and token.endswith("ed"):
        token = token[:-2]
    elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    if len(token) > 4 and token.endswith("e"):
        token = token[:-1]
    return token


def normalize(text: str) -> list[str]:
    """Searchable tokens of a text."""
    text = _ARABIC_MARKS.sub("", text).translate(_ARABIC_VARIANTS).lower()
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    tokens = []
    for token in _WORD.findall(text):
        if token in _STOPWORDS:
            continue
        if token.isascii() and token.isalpha():
            token = _stem(token)
        tokens.append(token)
    return tokens


@dataclass(frozen=True)
class Hit:
    source: str
    text: str
    score: float


class KnowledgeIndex:
    """BM25 index over one campaign's passages."""

    K1, B = 1.5, 0.75

    def __init__(self, passages: list[tuple[str, str]], languages: tuple[str, ...] = ()):
        self._passages = passages
        self.languages = languages  # languages the documents are written in
        self._tokens = [Counter(normalize(f"{source} {text}")) for source, text in passages]
        self._lengths = [sum(c.values()) for c in self._tokens]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for counts in self._tokens:
            document_frequency.update(counts.keys())
        n = len(passages)
        self._idf = {term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in document_frequency.items()}

    def __len__(self) -> int:
        return len(self._passages)

    def search(self, query: str, limit: int = 4) -> list[Hit]:
        """Best passages for the query, best first. Empty when nothing really matches:
        a weak match is left out, because a wrong passage invites a wrong answer."""
        terms = [t for t in dict.fromkeys(normalize(query)) if t in self._idf]
        if not terms:
            return []
        scored: list[tuple[float, int]] = []
        for i, counts in enumerate(self._tokens):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    norm = tf + self.K1 * (1 - self.B + self.B * self._lengths[i] / (self._average or 1))
                    score += self._idf[term] * tf * (self.K1 + 1) / norm
            if score > 0:
                scored.append((score, i))
        if not scored:
            return []
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        best = scored[0][0]
        hits = [Hit(self._passages[i][0], self._passages[i][1], round(score, 3)) for score, i in scored[:limit] if score >= best * 0.4]
        return hits


def load_index(s: Session, organization_id: int, campaign_id: int) -> KnowledgeIndex:
    """The index of one campaign's ready documents. Both ids are required and both are
    applied, so a campaign id from another organization finds nothing."""
    ready = (
        select(TrainingDocument.id, TrainingDocument.language)
        .where(
            TrainingDocument.organization_id == organization_id,
            TrainingDocument.campaign_id == campaign_id,
            TrainingDocument.status == "ready",
        )
    )
    documents = s.execute(ready).all()
    ids = [d.id for d in documents]
    if not ids:
        return KnowledgeIndex([])
    rows = s.execute(
        select(KnowledgeChunk.source_label, KnowledgeChunk.text)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeChunk.campaign_id == campaign_id,
            KnowledgeChunk.document_id.in_(ids),
        )
        .order_by(KnowledgeChunk.document_id, KnowledgeChunk.ordinal)
    ).all()
    languages = tuple(dict.fromkeys(d.language for d in documents if d.language))
    return KnowledgeIndex([(r.source_label, r.text) for r in rows], languages)
