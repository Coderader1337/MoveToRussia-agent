"""Retrieval-augmented answer chain: question -> Qdrant top-k -> DeepSeek answer.

Built with LangChain LCEL (RunnableParallel/RunnableLambda + ChatPromptTemplate)
so it composes cleanly with the retriever in retriever.py and the DeepSeek
chat model (OpenAI-compatible endpoint) below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from .config import settings
from .retriever import MtrKnowledgeBaseRetriever

SYSTEM_PROMPT = """You are an internal knowledge assistant for MoveToRussia.com managers.

You answer using ONLY the CONTEXT below (past client email precedents + internal FAQ).
This is not a client-facing message.

Content rules:
- Use only facts present in CONTEXT. Do not invent prices, timelines, guarantees, or links.
- If CONTEXT is insufficient or contradictory, add one short line starting with
  "Context gap:" and list what is missing. Do not guess.
- Always answer in English, regardless of the language used in CONTEXT excerpts.

Writing style (critical):
- State facts directly, as if you know the answer — not as a summary of documents.
- Never mention where the information came from: no "based on precedents/context/FAQ",
  "the internal FAQ states", "one client was told", "managers have advised", "in another case",
  "according to", "as mentioned in", "from the emails", etc.
- Do not describe the retrieval process. Just answer the manager's question.
- Sources are appended automatically — the answer body must contain zero source attribution.

Output format (strict — plain text only):
- No Markdown: no **, *, #, backticks, no bullet symbol •.
- No section headings ("Brief answer", "Important nuances", "Based on context", etc.).
- No follow-up questions. No invitations to ask more ("if you need clarification",
  "let me know", "feel free to ask", "please specify").
- Do NOT include "Sources" or source lists.
- Do NOT reference sources inline ("source 1", "ref 2", "thread_id=...").

Structure:
- Open with the direct answer (1–3 sentences).
- Then expand: requirements, steps, timelines, amounts, exceptions, family rules —
  everything relevant. Use numbered lists for sequences; short paragraphs for nuances.
- If important details exist in CONTEXT (costs, waiting times, documents), include them.
- Optional final line "Context gap: ..." only if data is genuinely missing.

Tone:
- Professional, clear, factual — like an internal reference note, not a literature review.
- Detailed and practical; every sentence must carry information.
- Plain text only: no Markdown (no **, *, #, backticks, •)."""

USER_TEMPLATE = """CONTEXT (past precedents / FAQ, most relevant first):
{context}

MANAGER QUESTION:
{question}"""


def sanitize_answer(text: str) -> str:
    """Strip Markdown and meta-attribution phrases if the model ignores format rules."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    # Drop common meta openers at the start of the answer.
    text = re.sub(
        r"^(Based on (?:the )?(?:precedents|context|FAQ|information|emails)[^.\n]*[.,]\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(According to (?:the )?(?:precedents|context|FAQ|information)[^.\n]*[.,]\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Drop trailing "ask me more" blocks the model sometimes adds.
    text = re.sub(
        r"\n+(If you need (to )?clarify|If you'd like (to )?clarify|Let me know|Feel free to ask).*$",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Drop duplicate sources block if model still outputs it.
    text = re.sub(r"\n+(Sources|Источники):.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def format_docs(docs: list[Document]) -> str:
    if not docs:
        return "(no relevant precedents found in the knowledge base)"
    blocks = []
    for i, doc in enumerate(docs, start=1):
        meta = doc.metadata
        header = f"[{i}] source={meta.get('source')} thread_id={meta.get('thread_id')}"
        if meta.get("subject"):
            header += f" subject=\"{meta['subject']}\""
        if meta.get("date_start"):
            header += f" date={meta['date_start']}"
        blocks.append(f"{header}\n{doc.page_content}")
    return "\n\n---\n\n".join(blocks)


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict] = field(default_factory=list)


def build_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.deepseek_temperature,
    )


def ask(
    question: str,
    *,
    top_k: int | None = None,
    source: str | None = None,
    exclude_low_signal: bool = False,
    manager_email: str | None = None,
    embedder: Embeddings | None = None,
) -> RagAnswer:
    """Convenience helper: run retrieval + generation and return answer + sources."""
    retriever_kwargs = dict(
        top_k=top_k or settings.retrieval_top_k,
        source=source,
        exclude_low_signal=exclude_low_signal,
        manager_email=manager_email,
    )
    if embedder is not None:
        retriever_kwargs["embedder"] = embedder
    retriever = MtrKnowledgeBaseRetriever(**retriever_kwargs)
    docs = retriever.invoke(question)

    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("human", USER_TEMPLATE)]
    )
    llm = build_llm()
    text_chain = prompt | llm | StrOutputParser()
    answer_text = sanitize_answer(
        text_chain.invoke({"context": format_docs(docs), "question": question})
    )

    sources = [
        {
            "thread_id": d.metadata.get("thread_id"),
            "subject": d.metadata.get("subject"),
            "source": d.metadata.get("source"),
            "date_start": d.metadata.get("date_start"),
            "score": d.metadata.get("score"),
        }
        for d in docs
    ]
    return RagAnswer(answer=answer_text, sources=sources)
