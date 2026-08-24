"""Extract factual RAG search questions from a manager request via DeepSeek."""

from __future__ import annotations

import json
import logging
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

EXTRACT_QUESTIONS_SYSTEM = """You help a MoveToRussia.com internal assistant retrieve facts from a knowledge base.

The manager may send either:
(A) A direct factual question (e.g. "What is the White Gloves package price?", "Какие документы нужны для Shared Values Visa?")
(B) A client email pasted by the manager plus optional drafting instructions

Extract ONLY the questions that need factual answers from the internal knowledge base (prices, fees, timelines, required documents, visa/residency types, policies, procedures, partner contacts, service scope, etc.).

You may also receive CONVERSATION HISTORY — the manager's previous messages in this thread and your previous answers. Use it ONLY to resolve references, pronouns, and ellipsis in the new MANAGER REQUEST (e.g. "and for his wife?", "what about the timeline?", "how much is that one?") into a fully self-contained query. Never treat CONVERSATION HISTORY itself as a factual source — the actual facts must still come from knowledge-base retrieval.

Rules:
- For (A): extract the manager's factual question as one or more search queries.
- For (B): include explicit AND implicit factual questions from the client's message.
- If the new MANAGER REQUEST refers back to a topic, person, or package mentioned earlier in CONVERSATION HISTORY, expand the reference so each query is understandable without the history.
- Write each question as a standalone English search query (even if the original was in another language).
- Do NOT include tone/style instructions, greetings, or meta requests like "draft a reply".
- Do NOT include questions answerable without company-specific facts.
- Merge duplicates; keep distinct topics as separate questions.
- Return 1-8 questions maximum.

Output: a JSON array of strings only, no markdown or commentary.
Example: ["What is the cost of the White Gloves package?", "What documents are required for Shared Values Visa?"]"""

EXTRACT_QUESTIONS_USER = """CONVERSATION HISTORY (previous turns in this thread, for resolving references only):
{history}

MANAGER REQUEST (new message to analyze):
{question}"""

NO_HISTORY_PLACEHOLDER = "(none — this is the first message in the thread)"


def format_history_for_prompt(history: list) -> str:
    """Отформатировать историю треда для промптов (extraction и generation).

    Принимает список объектов с полями `question` и `answer`
    (см. bot.user_state.HistoryTurn), без прямой зависимости от aiogram-слоя.
    """
    if not history:
        return NO_HISTORY_PLACEHOLDER
    blocks = []
    for i, turn in enumerate(history, start=1):
        blocks.append(
            f"[Turn {i}] Manager: {turn.question}\n[Turn {i}] Assistant: {turn.answer}"
        )
    return "\n\n".join(blocks)


def parse_questions_json(text: str) -> list[str]:
    """Parse a JSON string array of questions from model output."""
    text = text.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    if not isinstance(parsed, list):
        return []

    questions: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        question = item.strip()
        if not question:
            continue
        key = question.casefold()
        if key in seen:
            continue
        seen.add(key)
        questions.append(question)
    return questions


def extract_rag_questions(
    question: str,
    *,
    llm: ChatOpenAI,
    history: list | None = None,
    fallback_to_original: bool = True,
) -> list[str]:
    """Return factual search queries for knowledge-base retrieval.

    `history` — предыдущие реплики треда (bot.user_state.HistoryTurn), используются
    только для разрешения ссылок ("а по времени?"), не как источник фактов.
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", EXTRACT_QUESTIONS_SYSTEM),
            ("human", EXTRACT_QUESTIONS_USER),
        ]
    )
    chain = prompt | llm | StrOutputParser()
    raw = chain.invoke(
        {"question": question, "history": format_history_for_prompt(history or [])}
    )
    extracted = parse_questions_json(raw)

    if extracted:
        logger.info("Extracted %d RAG question(s)", len(extracted))
        return extracted

    logger.warning("Question extraction returned no parseable questions; using fallback")
    if fallback_to_original:
        fallback = question.strip()
        return [fallback] if fallback else []
    return []
