"""Extract factual RAG search questions from a manager request via DeepSeek."""

from __future__ import annotations

import json
import logging
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

EXTRACT_QUESTIONS_SYSTEM = """You help a MoveToRussia.com email drafting assistant retrieve facts from a knowledge base.

The manager sends a client email and optional drafting instructions. Extract ONLY the questions that need factual answers from the internal knowledge base (prices, fees, timelines, required documents, visa/residency types, policies, procedures, partner contacts, service scope, etc.).

Rules:
- Include explicit AND implicit factual questions from the client's message.
- Write each question as a standalone English search query (even if the client wrote in another language).
- Do NOT include tone/style instructions, greetings, or meta requests like "draft a reply".
- Do NOT include questions answerable without company-specific facts.
- Merge duplicates; keep distinct topics as separate questions.
- Return 1-8 questions maximum.

Output: a JSON array of strings only, no markdown or commentary.
Example: ["What is the cost of the White Gloves package?", "What documents are required for Shared Values Visa?"]"""

EXTRACT_QUESTIONS_USER = """MANAGER REQUEST:
{question}"""


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
    fallback_to_original: bool = True,
) -> list[str]:
    """Return factual search queries for knowledge-base retrieval."""
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", EXTRACT_QUESTIONS_SYSTEM),
            ("human", EXTRACT_QUESTIONS_USER),
        ]
    )
    chain = prompt | llm | StrOutputParser()
    raw = chain.invoke({"question": question})
    extracted = parse_questions_json(raw)

    if extracted:
        logger.info("Extracted %d RAG question(s)", len(extracted))
        return extracted

    logger.warning("Question extraction returned no parseable questions; using fallback")
    if fallback_to_original:
        fallback = question.strip()
        return [fallback] if fallback else []
    return []
