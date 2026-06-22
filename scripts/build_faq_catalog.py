"""
Справочник вопросов клиентов (v4) → CSV для Excel.

Вместо полной LLM-базы знаний v4: извлекаем вопросы клиентов, группируем похожие,
считаем frequency и подбираем лучший реальный ответ менеджера из переписки.
Редкие (1×), но развёрнутые ответы не отбрасываются — ранжируются по качеству.

Вход:  mailbox_export_clean/threads/*.txt  (очищенные переписки)
        + knowledge_base/clients_stats/Clients_stats_v4.csv
        Fallback: mailbox_export/all_messages.jsonl (--jsonl)
Выход: knowledge_base/v4/client_faq.csv                 — все сгруппированные вопросы
       knowledge_base/v4/client_faq_frequent.csv        — frequency ≥ 2 (основной для заказчика)
       knowledge_base/v4/client_faq_quality_once.csv    — редкие 1× с развёрнутым ответом
       knowledge_base/v4/client_faq_review.csv         — отфильтрованный список для разметки заказчиком
       knowledge_base/v4/client_faq_polished.csv         — после --llm (частые, отполированные)
       knowledge_base/v4/faq_build_stats.json
       knowledge_base/v4/_faq_intermediate/      — кэш шагов

Шаги:
  1) extract + cluster + csv  — быстро, без API
  2) --llm                    — полировка формулировок и ответов через DeepSeek

Примеры:
  python build_faq_catalog.py --force
  python build_faq_catalog.py --llm --force
  python build_faq_catalog.py --min-frequency 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_kb_versions import filter_messages, read_allowed_clients, write_manifest  # noqa: E402
from build_knowledge_base import (  # noqa: E402
    DEEPSEEK_TEMPERATURE,
    STOPWORDS,
    call_deepseek,
    group_threads,
    load_messages,
)

KB_V4 = ROOT / "knowledge_base" / "v4"
INTERMEDIATE = KB_V4 / "_faq_intermediate"
QA_PAIRS_PATH = INTERMEDIATE / "qa_pairs.jsonl"
CLUSTERS_PATH = INTERMEDIATE / "clusters.json"
STATS_PATH = KB_V4 / "faq_build_stats.json"
CSV_PATH = KB_V4 / "client_faq.csv"
CSV_FREQUENT_PATH = KB_V4 / "client_faq_frequent.csv"
CSV_RARE_PATH = KB_V4 / "client_faq_quality_once.csv"
CSV_REVIEW_PATH = KB_V4 / "client_faq_review.csv"
CSV_LLM_PATH = KB_V4 / "client_faq_polished.csv"

QUOTE_LINE = re.compile(r"^\s*>")
REPLY_CHAIN = re.compile(
    r"(?:^|\n)(?:-{3,}|_{3,}|={3,})\s*\n|"
    r"(?:^|\n)(?:Кому:|Тема:|Копия:|От:|"
    r"To:|Cc:|Bcc:|Subject:|From:|Sent:|Date:)\s|"
    r"(?:^|\n)On .+ wrote:\s*$|"
    r"(?:^|\n).+\@.+\.(?:com|ru|org|net).+ wrote:\s*$",
    re.I | re.M,
)
SIGNATURE_MARK = re.compile(
    r"\n(?:--+\s*\n|kind regards|warm regards|best regards|"
    r"client relationship manager|www\.movetorussia\.com)",
    re.I | re.M,
)
QUESTION_START = re.compile(
    r"^(?:what|how|when|where|why|who|can|could|would|do|does|did|is|are|was|were|"
    r"will|have|has|should|may|might|if|am i|are we|is it|is there|any)\b",
    re.I,
)
NOISE_QUESTION = re.compile(
    r"^(?:could you please correct|is that correct\??|is the accurate\??|"
    r"correct\??|dear |thank you|thanks |hi |hello |have a (?:wonderful|great|nice) |"
    r"wishing you|any updates will|if you don'?t want to receive|"
    r"\.{3,}|;\s*\d{2}\.\d{2}\.\d{4})",
    re.I,
)
MANAGER_TEMPLATE_Q = re.compile(
    r"(?:what is your (?:potential )?desired timeline|"
    r"are you in the exploration|have you already decided to relocate|"
    r"have you previously visited russia|"
    r"do you only need assistance with the relocation|"
    r"how many family members plan to move|"
    r"could you kindly provide a scan of your passport|"
    r"could you share a bit more (?:about )?your situation|"
    r"looking forward to your reply|please let us know your availability)",
    re.I,
)
EMAIL_HEADER_FRAGMENT = re.compile(
    r"(?:@\w+\.(?:com|ru|gmail|yandex)|\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}|"
    r"thank you kindly for your inquiry)",
    re.I,
)
CALL_ONLY_ANSWER = re.compile(
    r"^(?:please let us know your availability|would you be available for a (?:brief )?call|"
    r"as a next step, may i suggest we schedule a call|"
    r"if this service interests you, please feel free to contact us|"
    r"would you be available for a call|sent to you an invite for)",
    re.I,
)
SCHEDULING_QUESTION = re.compile(
    r"(?:work for you(?: for the|\b)|time slot|available for (?:a )?(?:call|onboarding)|"
    r"onboarding call|schedule a call|would (?:Thursday|Tuesday|Monday|Wednesday|Friday)|"
    r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)|"
    r"(?:UK|Moscow|Tbilisi|Ireland) time|join a meeting from my side)",
    re.I,
)
MANAGER_QUESTION_ANYWHERE = re.compile(
    r"(?:could you (?:please )?share a bit more about your situation|"
    r"what kind of timeline are you envisioning|"
    r"are you primarily looking for assistance|"
    r"plan to relocate to Russia and are seeking|"
    r"could you tell us a bit more about your family|"
    r"are you in the exploratory stage|"
    r"have you already decided to move to Russia|"
    r"could you kindly share a phone number|"
    r"would you like to explore options|"
    r"are you planning to relocate alone or with your family|"
    r"^\*?\s*are you planning to relocate\b|"
    r"is that correct\??$|"
    r"seeking support in obtaining a Temporary Residence Permit.*is that correct)",
    re.I,
)
PACKAGE_QUESTION = re.compile(
    r"(?:\b(?:cost|price|fee|pricing|retainer|how much)\b|"
    r"what is (?:the |your )?(?:cost|price)|"
    r"full[- ]service (?:package|relocation)|white gloves|"
    r"what specific support is included|support package|company cost|"
    r"price for your support)",
    re.I,
)
MANAGER_ASKS_CLIENT = re.compile(
    r"(?:could you kindly give me access|when you have a moment, could you kindly|"
    r"please find below answers to your questions|"
    r"to provide you with the best possible guidance|"
    r"to ensure we provide you with the most relevant information|"
    r"could you tell us a little more about your situation|"
    r"could you kindly share a bit more information about your situation|"
    r"suggested next step\b|"
    r"thank you kindly for answering the questions)",
    re.I,
)
CLIENT_NAME_PREFIX = re.compile(
    r"^[A-Z][a-z]+(?:,\s|\s+to ensure|\s+I am thinking|\s+could you kindly)",
)
CLIENT_VOICE_ANSWER = re.compile(
    r"^(?:I prefer to|I would like to|I was wanting|Thank you very much|"
    r"I am curious|I thought |I shared the|According to my latest)",
    re.I,
)
GENERIC_TOPIC_WORDS = frozenset({
    "visa", "residence", "permit", "trp", "prp", "shared", "values",
    "russia", "relocation", "relocate", "process", "program", "application",
    "temporary", "permanent", "golden", "move", "moving",
})
WHITE_GLOVES_TEMPLATE = re.compile(
    r"(?:White Gloves|Full-Service Support Package|success rate is 100%)",
    re.I,
)
EMAIL_NOISE_ANSWER = re.compile(
    r"(?:^To:\s|^Subject:|^CC:|^Cc:|^Bcc:|Beginning of forwarded message|"
    r"@\w+\.(?:com|ru|yandex|gmail)|Client relationship manager|"
    r"movetorussia\.com|arkvostok\.com|@\w+\s*<)",
    re.I | re.M,
)
SIGNOFF_LINE = re.compile(
    r"(?:^|\n)(?:--+\s*\n|------+\s*\n|Kind regards|Warm regards|Best regards|"
    r"Client relationship manager|www\.movetorussia\.com)",
    re.I,
)
# Вопросы без самостоятельного смысла (обрывки, ответы менеджеру, scheduling).
GENERIC_QUESTION = re.compile(
    r"(?:^anything else(?: i need to know| i should know)?\??$|"
    r"^is there anything else\b|"
    r"^outside of .+ anything else\b|"
    r"^anything else i'?m forgetting\b|"
    r"^please let me know if it'?s you need anything else\b|"
    r"^are you able to tell me more about this\??$|"
    r"^could you tell me more\b|"
    r"^tell me more about this\??$|"
    r"^is that (?:correct|accurate|right)\??$|"
    r"^does that (?:make sense|work)\??$|"
    r"^you will call via\b|"
    r"^work on your end\??$|"
    r"^perhaps this take place\b|"
    r"^can we please schedule a call\b|"
    r"^would you be available\b|"
    r"^eastern time work\b|"
    r"^what is .+'s phone number\b|"
    r"^and also what is\b|"
    r"^number two, what kind of timeline\b|"
    r"^have a (?:wonderful|great|nice)\b|"
    r"^thank you\b|"
    r"^correct\??$)",
    re.I,
)
FAQ_TOPIC_WORDS = frozenset({
    "visa", "residence", "permit", "trp", "prp", "citizenship", "passport",
    "document", "apostille", "criminal", "registration", "migration",
    "payment", "fee", "cost", "price", "invoice", "retainer", "deposit",
    "wire", "transfer", "bank", "broker", "investment", "golden",
    "shared", "values", "relocate", "relocation", "russia", "moscow",
    "family", "spouse", "children", "timeline", "process", "step",
    "hotel", "flight", "insurance", "medical", "language", "work", "job",
    "tax", "fbi", "apostille", "translation", "contract", "agreement",
    "eligibility", "permanent", "temporary", "tourist", "entry",
})
SALUTATION = re.compile(
    r"^(?:Dear|Hi|Hello|Good (?:morning|afternoon|evening)|Greetings)\s+"
    r"(?:Mr\.|Ms\.|Mrs\.|Miss|Dr\.)?\s*[^,\n]{1,60},\s*",
    re.I | re.M,
)
HONORIFIC_NAME = re.compile(
    r"\b(?:Mr\.|Ms\.|Mrs\.|Miss|Dr\.)\s+[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)?\b"
)
THANKS_OPENING = re.compile(
    r"^(?:Thank you(?: kindly)?(?: for your (?:reply|message|email|inquiry))?[^.!?]*[.!?]\s*)+",
    re.I,
)
BOILERPLATE_ANSWER = re.compile(
    r"^(?:hope you(?:'re| are) having a (?:wonderful|great|nice)|"
    r"i hope this (?:message|email) finds you|"
    r"looking forward to (?:hearing from you|your reply)|"
    r"please (?:let me know|don't hesitate)|"
    r"we are looking forward to support you|"
    r"understood\.?\s*it'?s wonderful|"
    r"apologies for my delayed response)",
    re.I,
)

LLM_SYSTEM = (
    "Ты методолог MoveToRussia.com. Из переписки клиентов и менеджеров составляешь "
    "строки FAQ для Excel. Для каждой группы:\n"
    "1) question — самодостаточный вопрос клиента на английском (с контекстом, не обрывок "
    "вроде «anything else?» или «tell me more about this»);\n"
    "2) answer — обезличенный эталонный ответ: БЕЗ Dear Mr./Ms., имён, подписей, "
    "«Kind regards»; нейтральный тон компании; только факты из исходных ответов;\n"
    "3) ничего не выдумывай; суммы, сроки, ссылки сохраняй.\n"
    "Ответ на английском."
)

LLM_BATCH_TEMPLATE = """Обработай каждую группу. Верни JSON-массив объектов:
{{"id": <номер группы>, "question": "...", "answer": "..."}}

Группы:
{batch}
"""


@dataclass
class QAPair:
    question: str
    answer: str
    client: str
    signature: frozenset[str]
    answer_score: float = 0.0


@dataclass
class Cluster:
    cluster_id: int
    frequency: int
    representative_question: str
    signature: frozenset[str]
    pairs: list[QAPair] = field(default_factory=list)
    best_answer: str = ""
    best_answer_score: float = 0.0
    question_variants: list[str] = field(default_factory=list)


def _trim_reply_chain(text: str) -> str:
    """Лёгкая обрезка: данные уже из mailbox_export_clean, только подпись."""
    if not text:
        return ""
    body = SIGNATURE_MARK.split(text, maxsplit=1)[0]
    return body.strip()


def _clean_body(text: str, *, multiline: bool = False) -> str:
    body = _trim_reply_chain(text or "")
    if not body:
        return ""
    if multiline:
        return body
    return re.sub(r"\s+", " ", body).strip()


def _sentences(text: str) -> list[str]:
    text = _clean_body(text, multiline=True)
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", flat)
    return [p.strip() for p in parts if p.strip()]


def _content_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-zA-Z']{3,}", text.lower())
    return frozenset(w for w in words if w not in STOPWORDS)


def _question_signature(q: str) -> frozenset[str]:
    words = re.findall(r"[a-zA-Z']{3,}", q.lower())
    sig = [w for w in words if w not in STOPWORDS]
    return frozenset(sig[:10])


def _content_word_count(s: str) -> int:
    words = re.findall(r"[a-zA-Z']{3,}", s.lower())
    return len([w for w in words if w not in STOPWORDS])


def _topic_word_hits(text: str) -> int:
    words = set(re.findall(r"[a-zA-Z']{3,}", text.lower()))
    return len(words & FAQ_TOPIC_WORDS)


def _question_quality_score(q: str) -> float:
    q = q.strip()
    score = _content_word_count(q) * 1.5 + _topic_word_hits(q) * 3.0
    if len(q) >= 60:
        score += 2.0
    if GENERIC_QUESTION.search(q):
        score -= 20.0
    if MANAGER_TEMPLATE_Q.search(q):
        score -= 15.0
    if _content_word_count(q) < 5:
        score -= 5.0
    if _topic_word_hits(q) == 0 and _content_word_count(q) < 8:
        score -= 8.0
    return score


def _strip_quotes(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if QUOTE_LINE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _incoming_text_for_questions(text: str) -> str:
    body = _strip_quotes(text)
    body = REPLY_CHAIN.split(body, maxsplit=1)[0]
    body = SIGNATURE_MARK.split(body, maxsplit=1)[0]
    return body.strip()


def _answer_has_substance(answer: str, question: str) -> bool:
    ans = _strip_question_echo(answer, question).strip()
    if len(ans) < 50:
        return False
    q_norm = re.sub(r"\W+", " ", question.lower()).strip()
    a_norm = re.sub(r"\W+", " ", ans.lower()).strip()
    if a_norm == q_norm:
        return False
    if q_norm in a_norm and len(a_norm) < len(q_norm) + 35:
        return False
    if ans.rstrip().endswith("?") and not re.search(
        r"\?\s+(?:Yes|No|Absolutely|Certainly|The |You |We |It |This |Regarding |Currently )",
        answer,
        re.I,
    ):
        return False
    return True


def _distinctive_words(text: str) -> frozenset[str]:
    return frozenset(w for w in _content_words(text) if w not in GENERIC_TOPIC_WORDS)


def _strip_question_echo(answer: str, question: str) -> str:
    """Убрать эхо вопроса из начала ответа (нумерованные FAQ-письма)."""
    if not answer:
        return answer
    q_key = re.sub(r"\s+", " ", question.lower())[:50]
    if q_key and q_key in answer.lower()[:160]:
        parts = re.split(r"\?\s+", answer, maxsplit=1)
        if len(parts) == 2 and len(parts[1].strip()) >= 40:
            return parts[1].strip()
    answer = re.sub(r"^\d+\.\s+", "", answer)
    return answer


def _answer_relevance(question: str, answer: str) -> float:
    q_words = _content_words(question)
    if not q_words:
        return 0.0
    a_words = _content_words(answer)
    overlap = len(q_words & a_words)
    score = overlap / max(min(len(q_words), 12), 1) * 10.0
    distinctive = _distinctive_words(question)
    if distinctive:
        d_overlap = len(distinctive & a_words)
        score += d_overlap * 4.0
        if d_overlap == 0 and len(distinctive) >= 2:
            score -= 8.0
    if overlap == 0 and _topic_word_hits(question) >= 1:
        q_topics = set(re.findall(r"[a-zA-Z']{3,}", question.lower())) & FAQ_TOPIC_WORDS
        a_topics = set(re.findall(r"[a-zA-Z']{3,}", answer.lower())) & FAQ_TOPIC_WORDS
        score += len(q_topics & a_topics) * 4.0
    if WHITE_GLOVES_TEMPLATE.search(answer) and not PACKAGE_QUESTION.search(question):
        score -= 10.0
    if EMAIL_NOISE_ANSWER.search(answer):
        score -= 15.0
    if CALL_ONLY_ANSWER.search(answer[:220]):
        score -= 6.0
    return score


def _combined_qa_score(question: str, answer: str, raw_score: float) -> float:
    return raw_score + _answer_relevance(question, answer) * 1.2


def _looks_like_question(s: str) -> bool:
    s = s.strip()
    if len(s) < 20 or len(s) > 420:
        return False
    if re.match(r"https?://", s):
        return False
    if re.match(r"^Hello,\s", s, re.I):
        return False
    if re.match(r"^Would you like to\b", s, re.I):
        return False
    if re.search(r"\bDear\s+[A-Z]", s):
        return False
    if re.search(r"^\d+(?:st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4}\b", s):
        return False
    if re.search(r"^\-\s*Are you planning to relocate\b", s, re.I):
        return False
    if re.search(r"\banything else i'?m forgetting\b", s, re.I):
        return False
    if re.search(r"\bdo i need to do anything else\b", s, re.I):
        return False
    if MANAGER_ASKS_CLIENT.search(s):
        return False
    if NOISE_QUESTION.search(s):
        return False
    if GENERIC_QUESTION.search(s):
        return False
    if MANAGER_TEMPLATE_Q.search(s):
        return False
    if MANAGER_QUESTION_ANYWHERE.search(s):
        return False
    if SCHEDULING_QUESTION.search(s):
        return False
    if EMAIL_HEADER_FRAGMENT.search(s) and len(s) > 120:
        return False
    if _content_word_count(s) < 5:
        return False
    if not s.endswith("?"):
        return False
    if _question_quality_score(s) < 4.0:
        return False
    return True


def _extract_questions(text: str) -> list[str]:
    found: list[str] = []
    for s in _sentences(text):
        if _looks_like_question(s):
            found.append(s)
    return found


def anonymize_answer(text: str) -> str:
    """Обезличить ответ менеджера для FAQ."""
    t = _clean_body(text, multiline=True)
    if not t:
        return ""
    t = REPLY_CHAIN.split(t, maxsplit=1)[0]
    t = re.split(r"(?i)beginning of forwarded message", t, maxsplit=1)[0]
    t = SIGNOFF_LINE.split(t, maxsplit=1)[0]
    t = SIGNATURE_MARK.split(t, maxsplit=1)[0]
    t = SALUTATION.sub("", t, count=1)
    t = re.sub(r"^[A-Z][a-z]+,\s+", "", t)
    t = re.sub(r"\bDear\s*,\s*", "", t, flags=re.I)
    t = THANKS_OPENING.sub("", t, count=1)
    t = HONORIFIC_NAME.sub("", t)
    t = re.sub(r"\b(?:Mr\.|Ms\.|Mrs\.|Miss|Dr\.)\s+", "", t)
    t = re.sub(
        r"\b(?:Belobragin|Naumenko|Antonova|Perry|Novik|Gridneva|Serebrennikov|Evgenija|Nadia|Anna)\b",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s+", " ", t).strip(" ,;")
    t = re.sub(r"\bDear\s*$", "", t, flags=re.I).strip(" ,;")
    return t


def _is_clean_faq_answer(text: str, question: str = "") -> bool:
    if not text or len(text) < 50:
        return False
    if EMAIL_NOISE_ANSWER.search(text):
        return False
    if SIGNOFF_LINE.search(text):
        return False
    if SALUTATION.search(text[:80]) or re.match(r"^Dear\s+", text, re.I):
        return False
    if CLIENT_VOICE_ANSWER.search(text):
        return False
    if not _answer_has_substance(text, question):
        return False
    if re.search(r"Best regards,\s+[A-Z][a-z]+\s+[A-Z][a-z]+$", text):
        return False
    if question and _answer_relevance(question, text) < 3.0:
        return False
    if question:
        distinctive = _distinctive_words(question)
        if distinctive:
            d_overlap = len(distinctive & _content_words(text))
            need = min(2, len(distinctive))
            if d_overlap < need:
                return False
    return True


def _answer_paragraphs(text: str) -> list[str]:
    raw = _clean_body(text, multiline=True)
    if not raw:
        return []
    parts = re.split(r"\n\s*\n+", raw)
    if len(parts) <= 1:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", raw)
    out: list[str] = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) < 40:
            continue
        if BOILERPLATE_ANSWER.search(p[:120]):
            continue
        if CALL_ONLY_ANSWER.search(p[:160]):
            continue
        if EMAIL_NOISE_ANSWER.search(p):
            continue
        out.append(p)
    return out


def extract_faq_answer(text: str, question: str) -> str:
    """Выбрать содержательную часть ответа и обезличить."""
    q_words = _question_signature(question)
    best = ""
    best_score = -999.0
    for para in _answer_paragraphs(text):
        anon = anonymize_answer(para)
        if len(anon) < 50:
            continue
        score = min(len(anon), 1200) / 100.0
        p_words = _question_signature(anon)
        overlap = len(q_words & p_words)
        score += overlap * 2.5
        if re.search(r"\d", anon):
            score += 1.0
        if BOILERPLATE_ANSWER.search(anon[:120]):
            score -= 3.0
        if WHITE_GLOVES_TEMPLATE.search(anon) and not PACKAGE_QUESTION.search(question):
            score -= 8.0
        if score > best_score:
            best_score = score
            best = anon
    if best:
        best = _strip_question_echo(best, question)
        return best[:1800] if _is_clean_faq_answer(best, question) else ""
    anon_full = anonymize_answer(text)
    anon_full = _strip_question_echo(anon_full, question)
    if len(anon_full) >= 50 and _is_clean_faq_answer(anon_full, question):
        return anon_full[:1800]
    return ""


def _score_answer(text: str, question: str = "", *, extracted: str = "") -> float:
    faq = extracted or (extract_faq_answer(text, question) if question else anonymize_answer(text))
    if not faq or len(faq) < 50:
        return 0.0
    score = min(len(faq), 1800) / 100.0
    if re.search(r"\d", faq):
        score += 1.5
    if re.search(r"[-•*]\s", faq):
        score += 2.0
    if re.search(r"https?://", faq):
        score += 1.0
    if _topic_word_hits(faq) >= 2:
        score += 2.0
    if SALUTATION.search(text[:120]) or HONORIFIC_NAME.search(text[:200]):
        score -= 1.0  # исходник грязный, но извлекли чистое
    if CALL_ONLY_ANSWER.search(faq[:220]):
        score -= 5.0
    if BOILERPLATE_ANSWER.search(faq[:160]):
        score -= 3.0
    if EMAIL_NOISE_ANSWER.search(faq):
        score -= 8.0
    if question:
        score += _answer_relevance(question, faq) * 0.5
    return score


def _pick_reply_text(
    thread: list[dict[str, Any]], after_idx: int, question: str
) -> str:
    candidates: list[tuple[float, str]] = []
    checked = 0
    for m in thread[after_idx + 1 :]:
        if m.get("direction") != "outgoing":
            continue
        checked += 1
        if checked > 4:
            break
        raw = m.get("text", "") or ""
        faq = extract_faq_answer(raw, question)
        if not faq or not _is_clean_faq_answer(faq, question):
            continue
        score = _combined_qa_score(question, faq, _score_answer(raw, question, extracted=faq))
        candidates.append((score, faq))
    if not candidates:
        return ""
    best_score, best = max(candidates, key=lambda x: x[0])
    if _answer_relevance(question, best) < 3.5:
        return ""
    return best


def extract_qa_pairs(
    threads: dict[str, list[dict[str, Any]]],
) -> list[QAPair]:
    pairs: list[QAPair] = []
    for client, thread in threads.items():
        for idx, msg in enumerate(thread):
            if msg.get("direction") != "incoming":
                continue
            for q in _extract_questions(_incoming_text_for_questions(msg.get("text", "") or "")):
                answer = _pick_reply_text(thread, idx, q)
                if not answer:
                    continue
                sig = _question_signature(q)
                if len(sig) < 2:
                    continue
                pair = QAPair(
                    question=q,
                    answer=answer,
                    client=client,
                    signature=sig,
                    answer_score=_combined_qa_score(q, answer, _score_answer(answer, q, extracted=answer)),
                )
                pairs.append(pair)
    return pairs


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _should_merge(a: frozenset[str], b: frozenset[str], threshold: float) -> bool:
    j = _jaccard(a, b)
    if j >= threshold:
        return True
    inter = len(a & b)
    if inter >= 3 and inter / min(len(a), len(b)) >= 0.55:
        return True
    return False


def cluster_pairs(pairs: list[QAPair], *, threshold: float) -> list[Cluster]:
    n = len(pairs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge(pairs[i].signature, pairs[j].signature, threshold):
                union(i, j)

    groups: dict[int, list[QAPair]] = defaultdict(list)
    for i, p in enumerate(pairs):
        groups[find(i)].append(p)

    clusters: list[Cluster] = []
    for cid, (root, group) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1):
        q_counter = Counter(p.question for p in group)
        rep_q = max(q_counter.keys(), key=_question_quality_score)
        sig = frozenset()
        for p in group:
            sig = sig | p.signature
        best = max(
            group,
            key=lambda p: (_answer_relevance(rep_q, p.answer), p.answer_score, len(p.answer)),
        )
        variants = sorted(q_counter.keys(), key=_question_quality_score, reverse=True)[:5]
        best_answer = best.answer
        clusters.append(
            Cluster(
                cluster_id=cid,
                frequency=len(group),
                representative_question=rep_q,
                signature=sig,
                pairs=group,
                best_answer=best_answer,
                best_answer_score=best.answer_score,
                question_variants=variants,
            )
        )

    clusters.sort(key=lambda c: (-c.frequency, -c.best_answer_score, c.representative_question))
    for i, c in enumerate(clusters, start=1):
        c.cluster_id = i
    return clusters


def filter_clusters(clusters: list[Cluster], *, min_answer_score: float) -> list[Cluster]:
    return [c for c in clusters if is_review_quality(c)]


def is_review_quality(c: Cluster) -> bool:
    q = c.representative_question
    a = c.best_answer
    if _question_quality_score(q) < 12.0:
        return False
    if _topic_word_hits(q) == 0 and _content_word_count(q) < 8:
        return False
    if MANAGER_QUESTION_ANYWHERE.search(q) or SCHEDULING_QUESTION.search(q):
        return False
    if MANAGER_ASKS_CLIENT.search(q) or CLIENT_NAME_PREFIX.search(q):
        return False
    if len(a) < 80:
        return False
    if c.best_answer_score < 8.0:
        return False
    rel = _answer_relevance(q, a)
    if rel < 6.0:
        return False
    if GENERIC_QUESTION.search(q):
        return False
    if re.search(r"\banything else i'?m forgetting\b", q, re.I):
        return False
    if CALL_ONLY_ANSWER.search(a[:240]):
        return False
    if not _is_clean_faq_answer(a, q):
        return False
    if not _answer_has_substance(a, q):
        return False
    if WHITE_GLOVES_TEMPLATE.search(a) and not PACKAGE_QUESTION.search(q):
        return False
    return True


def write_csv(clusters: list[Cluster], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(["number", "question", "frequency", "answer"])
        for i, c in enumerate(clusters, start=1):
            w.writerow([
                i,
                c.representative_question,
                c.frequency,
                c.best_answer,
            ])


def _is_quality_singleton(c: Cluster, *, min_score: float, min_chars: int) -> bool:
    return is_review_quality(c)


def split_outputs(clusters: list[Cluster], *, min_frequency: int, rare_min_score: float,
                  rare_min_chars: int) -> tuple[list[Cluster], list[Cluster], list[Cluster]]:
    frequent = [c for c in clusters if c.frequency >= min_frequency]
    rare = [
        c for c in clusters
        if c.frequency == 1 and _is_quality_singleton(
            c, min_score=rare_min_score, min_chars=rare_min_chars,
        )
    ]
    review = sorted(frequent + rare, key=lambda c: (-c.frequency, -c.best_answer_score))
    return frequent, rare, review


def save_intermediate(pairs: list[QAPair], clusters: list[Cluster]) -> None:
    INTERMEDIATE.mkdir(parents=True, exist_ok=True)
    with QA_PAIRS_PATH.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(
                json.dumps(
                    {
                        "question": p.question,
                        "answer": p.answer,
                        "client": p.client,
                        "signature": sorted(p.signature),
                        "answer_score": p.answer_score,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    payload = [
        {
            "cluster_id": c.cluster_id,
            "frequency": c.frequency,
            "representative_question": c.representative_question,
            "question_variants": c.question_variants,
            "best_answer": c.best_answer,
            "best_answer_score": c.best_answer_score,
        }
        for c in clusters
    ]
    CLUSTERS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_clusters_from_cache() -> list[Cluster]:
    data = json.loads(CLUSTERS_PATH.read_text(encoding="utf-8"))
    clusters: list[Cluster] = []
    for row in data:
        clusters.append(
            Cluster(
                cluster_id=row["cluster_id"],
                frequency=row["frequency"],
                representative_question=row["representative_question"],
                signature=frozenset(),
                best_answer=row["best_answer"],
                best_answer_score=row["best_answer_score"],
                question_variants=row.get("question_variants", []),
            )
        )
    return clusters


def _llm_batch(clusters: list[Cluster], *, temperature: float) -> list[dict[str, Any]]:
    lines: list[str] = []
    for c in clusters:
        variants = " | ".join(c.question_variants[:3])
        answers = sorted({p.answer[:1200] for p in c.pairs}, key=len, reverse=True)[:3]
        if not answers:
            answers = [c.best_answer[:1200]]
        ans_block = "\n---\n".join(answers)
        lines.append(
            f"ID {c.cluster_id} (frequency={c.frequency})\n"
            f"Варианты вопроса: {variants}\n"
            f"Ответы менеджеров:\n{ans_block}\n"
        )
    user = LLM_BATCH_TEMPLATE.format(batch="\n\n".join(lines))
    raw = call_deepseek(LLM_SYSTEM, user, temperature=temperature)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def polish_with_llm(
    clusters: list[Cluster],
    *,
    temperature: float,
    batch_size: int = 8,
) -> list[Cluster]:
    polished: list[Cluster] = []
    total_batches = (len(clusters) + batch_size - 1) // batch_size
    for bi, start in enumerate(range(0, len(clusters), batch_size), start=1):
        batch = clusters[start : start + batch_size]
        print(f"  LLM batch {bi}/{total_batches} ({len(batch)} групп)…")
        t0 = time.time()
        try:
            results = _llm_batch(batch, temperature=temperature)
        except Exception as exc:
            print(f"    ошибка LLM: {exc}", file=sys.stderr)
            polished.extend(batch)
            continue
        by_id = {int(r["id"]): r for r in results if "id" in r}
        for c in batch:
            row = by_id.get(c.cluster_id)
            if row:
                c.representative_question = (row.get("question") or c.representative_question).strip()
                c.best_answer = (row.get("answer") or c.best_answer).strip()
            polished.append(c)
        print(f"    готово за {time.time() - t0:.0f} c")
        time.sleep(1)
    return polished


def _copy_cluster(c: Cluster) -> Cluster:
    return Cluster(
        cluster_id=c.cluster_id,
        frequency=c.frequency,
        representative_question=c.representative_question,
        signature=c.signature,
        pairs=list(c.pairs),
        best_answer=c.best_answer,
        best_answer_score=c.best_answer_score,
        question_variants=list(c.question_variants),
    )


def write_stats(
    *,
    qa_pairs_count: int,
    clusters_all: list[Cluster],
    clusters_out: list[Cluster],
    clusters_frequent: int,
    clusters_rare: int,
    clusters_review: int,
    allowed_clients: int,
    messages: int,
    llm_polished: bool,
    message_source: str,
) -> None:
    freq_hist = Counter(c.frequency for c in clusters_out)
    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "message_source": message_source,
        "clients_in_scope": allowed_clients,
        "messages_in_scope": messages,
        "qa_pairs_extracted": qa_pairs_count,
        "clusters_before_filter": len(clusters_all),
        "clusters_in_csv_full": len(clusters_out),
        "clusters_frequent": clusters_frequent,
        "clusters_rare_quality": clusters_rare,
        "clusters_review": clusters_review,
        "singleton_clusters": sum(1 for c in clusters_out if c.frequency == 1),
        "frequency_distribution": dict(sorted(freq_hist.items())),
        "llm_polished": llm_polished,
        "outputs": {
            "csv_full": str(CSV_PATH.relative_to(ROOT)),
            "csv_frequent": str(CSV_FREQUENT_PATH.relative_to(ROOT)),
            "csv_rare_quality": str(CSV_RARE_PATH.relative_to(ROOT)),
            "csv_review": str(CSV_REVIEW_PATH.relative_to(ROOT)),
            "csv_polished": str(CSV_LLM_PATH.relative_to(ROOT)),
            "intermediate": str(INTERMEDIATE.relative_to(ROOT)),
        },
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm", action="store_true",
                   help="Полировка question/answer через DeepSeek → client_faq_polished.csv")
    p.add_argument("--jsonl", action="store_true",
                   help="Брать письма из mailbox_export/all_messages.jsonl вместо clean")
    p.add_argument("--force", action="store_true", help="Пересобрать extract/cluster без кэша")
    p.add_argument("--cluster-threshold", type=float, default=0.34,
                   help="Порог похожести вопросов (Jaccard), по умолчанию 0.34")
    p.add_argument("--min-answer-score", type=float, default=6.0,
                   help="Мин. качество ответа для включения в полный CSV (1×)")
    p.add_argument("--min-frequency", type=int, default=2,
                   help="Порог frequency для client_faq_frequent.csv")
    p.add_argument("--rare-min-score", type=float, default=10.0,
                   help="Мин. score ответа для client_faq_quality_once.csv")
    p.add_argument("--rare-min-chars", type=int, default=120,
                   help="Мин. длина ответа для client_faq_quality_once.csv")
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    p.add_argument("--llm-batch-size", type=int, default=8)
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("=== FAQ-справочник v4 ===")
    print("Загрузка писем…")
    all_messages = load_messages(prefer_clean=not args.jsonl)
    allowed, flagged_n, _ = read_allowed_clients(4)
    messages = filter_messages(all_messages, allowed)
    threads = group_threads(messages)
    print(f"  адресов: {len(allowed)} | писем: {len(messages)} | диалогов: {len(threads)}")

    KB_V4.mkdir(parents=True, exist_ok=True)
    write_manifest(KB_V4, messages)

    clusters_all: list[Cluster]
    if QA_PAIRS_PATH.is_file() and CLUSTERS_PATH.is_file() and not args.force:
        print("Extract/cluster: из кэша (_faq_intermediate/)")
        clusters = load_clusters_from_cache()
        qa_pairs_count = sum(1 for _ in QA_PAIRS_PATH.open(encoding="utf-8"))
        clusters_all = clusters
    else:
        print("Extract: вопросы клиентов + ответы менеджеров…")
        pairs = extract_qa_pairs(threads)
        qa_pairs_count = len(pairs)
        print(f"  пар Q→A: {qa_pairs_count}")
        print(f"Cluster: порог {args.cluster_threshold}…")
        clusters_all = cluster_pairs(pairs, threshold=args.cluster_threshold)
        clusters = filter_clusters(clusters_all, min_answer_score=args.min_answer_score)
        print(f"  кластеров всего: {len(clusters_all)} → в CSV: {len(clusters)}")
        save_intermediate(pairs, clusters)

    write_csv(clusters, CSV_PATH)
    print(f"CSV (полный): {CSV_PATH} ({len(clusters)} строк)")

    frequent, rare, review = split_outputs(
        clusters,
        min_frequency=args.min_frequency,
        rare_min_score=args.rare_min_score,
        rare_min_chars=args.rare_min_chars,
    )
    write_csv(frequent, CSV_FREQUENT_PATH)
    write_csv(rare, CSV_RARE_PATH)
    print(f"CSV (частые, freq≥{args.min_frequency}): {CSV_FREQUENT_PATH} ({len(frequent)} строк)")
    print(f"CSV (редкие качественные 1×): {CSV_RARE_PATH} ({len(rare)} строк)")

    review = [c for c in clusters if is_review_quality(c)]
    review.sort(key=lambda c: (-c.frequency, -c.best_answer_score))
    write_csv(review, CSV_REVIEW_PATH)
    print(f"CSV (review для заказчика): {CSV_REVIEW_PATH} ({len(review)} строк)")

    llm_polished = False
    llm_input = review
    if args.llm:
        llm_source = review if review else frequent
        print(f"LLM: полировка ({len(llm_source)} групп)…")
        llm_input = polish_with_llm(
            [_copy_cluster(c) for c in llm_source],
            temperature=args.temperature,
            batch_size=args.llm_batch_size,
        )
        write_csv(llm_input, CSV_LLM_PATH)
        print(f"CSV (LLM): {CSV_LLM_PATH} ({len(llm_input)} строк)")
        llm_polished = True

    write_stats(
        qa_pairs_count=qa_pairs_count,
        clusters_all=clusters_all,
        clusters_out=clusters,
        clusters_frequent=len(frequent),
        clusters_rare=len(rare),
        clusters_review=len(review),
        allowed_clients=len(allowed),
        messages=len(messages),
        llm_polished=llm_polished,
        message_source="jsonl" if args.jsonl else "mailbox_export_clean",
    )

    print(f"Статистика: {STATS_PATH}")
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
