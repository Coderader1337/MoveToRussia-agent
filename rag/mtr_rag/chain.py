"""Retrieval-augmented email drafting: extract questions -> Qdrant top-k -> DeepSeek client email.

Flow:
1. DeepSeek extracts factual RAG search questions from the manager request.
2. Each question retrieves precedents (Voyage + Qdrant); results are merged.
3. DeepSeek drafts the manager-facing analysis + client email using CONTEXT.
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
from .mail_writing_prompt import USER_TEMPLATE, build_system_prompt, load_communication_principles
from .question_extraction import extract_rag_questions
from .retriever import MtrKnowledgeBaseRetriever


def sanitize_answer(text: str) -> str:
    """Light cleanup of the full manager-facing response (analysis + draft email)."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
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


def format_rag_questions(questions: list[str]) -> str:
    if not questions:
        return "(none)"
    return "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))


def _doc_dedupe_key(doc: Document) -> str:
    meta = doc.metadata
    thread_id = meta.get("thread_id") or ""
    source = meta.get("source") or ""
    return f"{source}:{thread_id}:{hash(doc.page_content)}"


def retrieve_merged(
    retriever: MtrKnowledgeBaseRetriever,
    queries: list[str],
    *,
    top_k: int,
) -> list[Document]:
    """Retrieve for one or more queries and return the best unique chunks."""
    if not queries:
        return []

    if len(queries) == 1:
        return retriever.invoke(queries[0])[:top_k]

    best: dict[str, Document] = {}
    for query in queries:
        for doc in retriever.invoke(query):
            key = _doc_dedupe_key(doc)
            prev = best.get(key)
            if prev is None or (doc.metadata.get("score") or 0) > (prev.metadata.get("score") or 0):
                best[key] = doc

    ranked = sorted(
        best.values(),
        key=lambda doc: doc.metadata.get("score") or 0,
        reverse=True,
    )
    return ranked[:top_k]


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    extracted_questions: list[str] = field(default_factory=list)


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
    """Extract factual questions, retrieve precedents + FAQ, then draft a client email."""
    effective_top_k = top_k or settings.retrieval_top_k
    llm = build_llm()
    rag_questions = extract_rag_questions(question, llm=llm)

    retriever_kwargs = dict(
        top_k=effective_top_k,
        source=source,
        exclude_low_signal=exclude_low_signal,
        manager_email=manager_email,
    )
    if embedder is not None:
        retriever_kwargs["embedder"] = embedder
    retriever = MtrKnowledgeBaseRetriever(**retriever_kwargs)
    docs = retrieve_merged(retriever, rag_questions, top_k=effective_top_k)

    prompt = ChatPromptTemplate.from_messages(
        [("system", build_system_prompt()), ("human", USER_TEMPLATE)]
    )
    text_chain = prompt | llm | StrOutputParser()
    answer_text = sanitize_answer(
        text_chain.invoke(
            {
                "context": format_docs(docs),
                "question": question,
                "rag_questions": format_rag_questions(rag_questions),
            }
        )
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
    return RagAnswer(
        answer=answer_text,
        sources=sources,
        extracted_questions=rag_questions,
    )


def warmup_prompt() -> None:
    """Load communication principles at startup (fail fast if file missing)."""
    load_communication_principles()
    build_system_prompt()
