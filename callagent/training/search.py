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

import logging
import math
import re
import threading
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import KnowledgeChunk, TrainingDocument

log = logging.getLogger(__name__)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ARABIC_MARKS = re.compile(r"[ً-ٰٟـ]")  # diacritics and tatweel
_ARABIC_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
# Arabic-Indic and Persian digits are the same numbers: "٥٠٠" is 500.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# The article, alone or behind a one-letter particle: "السعر", "بالسعر", "والسعر", "للسعر" are all "سعر".
_ARABIC_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال")
# Words too common to tell passages apart; BM25 already discounts them, dropping them keeps queries sharp.
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from has have how i in is it of on or our that the this to we what "
    "when where which who will with you your can de des du et la le les un une est que qui pour dans vous nous "
    "much many any some there their they them about please tell me my us if so not no yes would could should "
    "get need want know also just than then into out up there's it's "
    "au aux ce ces cette en sur par ne pas plus quel quelle quels quelles avec sont il elle on se sa son ses leur y "
    "vos votre nos notre mes mon ma tes ton ta je tu ils elles "
    "في من على الى عن هل ما هو هي هذا هذه ذلك التي الذي او ان كان مع كل لا نعم لكم لنا".split()
)
# Words that mean the same thing to someone asking about an offer, in the three
# languages calls are held in. A question about "prix" must find a page headed
# "Tarifs", and "refund" a "remboursement". Each group becomes one search term.
# This is a short list of commercial vocabulary, not a thesaurus; meaning beyond
# it needs a semantic index, which is not built.
_CONCEPT_GROUPS = (
    ("price", "pricing", "cost", "fee", "rate", "tarif", "prix", "cout", "tarification", "سعر", "اسعار", "ثمن", "تكلفه", "رسوم"),
    ("refund", "reimbursement", "remboursement", "rembourser", "استرداد", "استرجاع"),
    ("guarantee", "warranty", "garantie", "ضمان"),
    ("delivery", "shipping", "livraison", "expedition", "توصيل", "شحن"),
    ("contract", "agreement", "contrat", "engagement", "عقد", "اتفاقيه"),
    ("cancel", "cancellation", "terminate", "annuler", "annulation", "resilier", "resiliation", "الغاء"),
    ("discount", "promotion", "remise", "reduction", "rabais", "خصم", "تخفيض"),
    ("trial", "demo", "essai", "demonstration", "تجربه", "تجريبي"),
    ("subscription", "plan", "abonnement", "forfait", "formule", "اشتراك", "باقه"),
    ("support", "assistance", "helpdesk", "دعم", "مساعده"),
    ("invoice", "billing", "bill", "facture", "facturation", "فاتوره", "فواتير"),
    ("payment", "pay", "paiement", "payer", "reglement", "دفع", "سداد"),
)


def _stem(token: str) -> str:
    """Crude English/French suffix stripping so price, prices and pricing meet, as do
    post, posted and posting, and tarif, tarifs. Over-stemming costs nothing here: it
    only widens recall."""
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 4 and token.endswith("ed"):
        token = token[:-2]
    elif len(token) > 4 and token.endswith("aux"):
        token = token[:-3] + "al"  # tarifaux, journaux
    elif len(token) > 3 and token.endswith(("s", "x")) and not token.endswith("ss"):
        token = token[:-1]
    if len(token) > 4 and token.endswith("e"):
        token = token[:-1]
    return token


def _arabic_stem(token: str) -> str:
    for prefix in _ARABIC_PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            return token[len(prefix):]
    return token


def _base(token: str) -> str:
    if token.isascii():
        return _stem(token) if token.isalpha() else token
    return _arabic_stem(token)


_CONCEPT_OF = {_base(word): f"={group[0]}" for group in _CONCEPT_GROUPS for word in group}


def normalize(text: str) -> list[str]:
    """Searchable tokens of a text."""
    # Digits first: the range of Arabic marks removed next would take the Arabic-Indic digits with it.
    text = _ARABIC_MARKS.sub("", text.translate(_DIGITS)).translate(_ARABIC_VARIANTS).lower()
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    tokens = []
    for token in _WORD.findall(text):
        if token in _STOPWORDS or (len(token) == 1 and not token.isdigit()):
            continue  # a lone letter is what is left of l'offre or d'essai
        token = _base(token)
        if token in _STOPWORDS:
            continue  # the article stripped off a stop word leaves a stop word
        tokens.append(_CONCEPT_OF.get(token, token))
    return tokens


@dataclass(frozen=True)
class Hit:
    source: str
    text: str
    score: float


class KnowledgeIndex:
    """BM25 index over one campaign's passages. Built once and never changed after, so
    one index can serve every call of its campaign at the same time."""

    K1, B = 1.5, 0.75
    # A passage must account for at least a third of what the question asks about
    # (its words weighted by how telling each is). Below it the match is a coincidence
    # of common words, and reading it out would be answering a different question.
    MIN_COVERAGE = 0.34

    def __init__(self, passages: list[tuple[str, str]], languages: tuple[str, ...] = ()):
        self._passages = passages
        self.languages = languages  # languages the documents are written in
        counts = [Counter(normalize(f"{source} {text}")) for source, text in passages]
        self._lengths = [sum(c.values()) for c in counts]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        # term -> the passages that contain it, with how often: a search touches only those.
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for i, passage_counts in enumerate(counts):
            for term, frequency in passage_counts.items():
                self._postings.setdefault(term, []).append((i, frequency))
        n = len(passages)
        self._idf = {term: math.log(1 + (n - len(found) + 0.5) / (len(found) + 0.5)) for term, found in self._postings.items()}
        self._rarest = max(self._idf.values(), default=0.0)

    def __len__(self) -> int:
        return len(self._passages)

    def search(self, query: str, limit: int = 4) -> list[Hit]:
        """Best passages for the query, best first. Empty when nothing really matches:
        a weak match is left out, because a wrong passage invites a wrong answer."""
        asked = list(dict.fromkeys(normalize(query)))
        if not asked or not self._passages:
            return []
        # A word the documents never use is as telling as their rarest word, and no passage has it.
        weight = {term: self._idf.get(term, self._rarest) for term in asked}
        everything = sum(weight.values()) or 1.0
        score: dict[int, float] = {}
        covered: dict[int, float] = {}
        for term in asked:
            for i, tf in self._postings.get(term, ()):
                norm = tf + self.K1 * (1 - self.B + self.B * self._lengths[i] / (self._average or 1))
                score[i] = score.get(i, 0.0) + self._idf[term] * tf * (self.K1 + 1) / norm
                covered[i] = covered.get(i, 0.0) + weight[term]
        ranked = sorted(((value, i) for i, value in score.items() if covered[i] / everything >= self.MIN_COVERAGE), key=lambda pair: (-pair[0], pair[1]))
        if not ranked:
            return []
        best = ranked[0][0]
        return [Hit(self._passages[i][0], self._passages[i][1], round(value, 3)) for value, i in ranked[:limit] if value >= best * 0.4]


# An index is expensive to build (seconds for thousands of passages) and a campaign's
# documents change rarely, so the last one built is kept per campaign and reused by
# every call until its documents change.
MAX_PASSAGES = 20_000  # per campaign: beyond this the newest documents are searched and a warning is logged
_CACHE_SIZE = 64  # campaigns
_cache: "OrderedDict[tuple[int, int], tuple[tuple, KnowledgeIndex]]" = OrderedDict()
_cache_lock = threading.Lock()
builds = 0  # how many indexes were built, for tests and metrics


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def load_index(s: Session, organization_id: int, campaign_id: int) -> KnowledgeIndex:
    """The index of one campaign's ready documents. Both ids are required and both are
    applied, so a campaign id from another organization finds nothing.

    Reused from the last call when the campaign's ready documents are the same ones,
    read at the same moment; any upload, re-read or removal gives a new index."""
    global builds
    ready = (
        select(TrainingDocument.id, TrainingDocument.language, TrainingDocument.processed_at, TrainingDocument.chunk_count)
        .where(
            TrainingDocument.organization_id == organization_id,
            TrainingDocument.campaign_id == campaign_id,
            TrainingDocument.status == "ready",
        )
        .order_by(TrainingDocument.id)
    )
    documents = s.execute(ready).all()
    ids = [d.id for d in documents]
    if not ids:
        return KnowledgeIndex([])
    key = (organization_id, campaign_id)
    state = tuple((d.id, d.processed_at, d.chunk_count) for d in documents)
    with _cache_lock:
        kept = _cache.get(key)
        if kept is not None and kept[0] == state:
            _cache.move_to_end(key)
            return kept[1]
    rows = s.execute(
        select(KnowledgeChunk.source_label, KnowledgeChunk.text)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeChunk.campaign_id == campaign_id,
            KnowledgeChunk.document_id.in_(ids),
        )
        .order_by(KnowledgeChunk.document_id.desc(), KnowledgeChunk.ordinal)
        .limit(MAX_PASSAGES + 1)
    ).all()
    if len(rows) > MAX_PASSAGES:
        log.warning("campaign %s has more than %s passages; only the newest documents are searched on calls", campaign_id, MAX_PASSAGES)
        rows = rows[:MAX_PASSAGES]
    languages = tuple(dict.fromkeys(d.language for d in documents if d.language))
    index = KnowledgeIndex([(r.source_label, r.text) for r in rows], languages)
    with _cache_lock:
        builds += 1
        _cache[key] = (state, index)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return index
