"""Retrieval-augmented assistant: extract questions -> Qdrant top-k -> DeepSeek answer or email draft.

Flow:
1. DeepSeek extracts factual RAG search questions from the manager request.
2. Each question retrieves precedents (Voyage + Qdrant); results are merged.
3. DeepSeek responds: direct Q&A for factual questions, or analysis + client email draft
   when the manager pasted a client message and asked for a reply.
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
from .question_extraction import extract_rag_questions, format_history_for_prompt
from .retriever import MtrKnowledgeBaseRetriever
from .yandex_disk_sync import DISK_PRIORITY, DISK_SOURCE


def sanitize_answer(text: str) -> str:
    """Light cleanup of the full manager-facing response (analysis + draft email)."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n+(Sources|Источники):.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def _doc_sort_key(doc: Document) -> tuple[int, float]:
    """Official Yandex Disk files first, then by retrieval score."""
    priority_rank = 0 if doc.metadata.get("source") == DISK_SOURCE else 1
    return priority_rank, -(doc.metadata.get("score") or 0)


def _format_doc_header(index: int, meta: dict) -> str:
    source = meta.get("source")
    if source == DISK_SOURCE:
        file_path = meta.get("file_path") or meta.get("subject") or "unknown"
        priority = meta.get("priority") or DISK_PRIORITY
        return f'[{index}] source=official_file priority={priority} file="{file_path}"'
    header = f"[{index}] source={source} thread_id={meta.get('thread_id')}"
    if meta.get("subject"):
        header += f" subject=\"{meta['subject']}\""
    if meta.get("date_start"):
        header += f" date={meta['date_start']}"
    return header


def format_docs(docs: list[Document]) -> str:
    if not docs:
        return "(no relevant precedents found in the knowledge base)"
    blocks = []
    for i, doc in enumerate(sorted(docs, key=_doc_sort_key), start=1):
        header = _format_doc_header(i, doc.metadata)
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

    embed_queries = getattr(retriever.embedder, "embed_queries", None)
    if embed_queries is not None:
        vectors = embed_queries(queries)
    else:
        vectors = [retriever.embedder.embed_query(query) for query in queries]

    best: dict[str, Document] = {}
    for vector in vectors:
        for doc in retriever.search_by_vector(vector):
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
    history: list | None = None,
) -> RagAnswer:
    """Extract factual questions, retrieve precedents + FAQ, then answer or draft a client email.

    `history` — предыдущие реплики треда (bot.user_state.HistoryTurn или совместимый
    объект с полями question/answer). Используется для разрешения ссылок при извлечении
    RAG-вопросов и для связности генерации; ретривал в Qdrant при этом выполняется
    заново на каждом запросе — история не заменяет и не кэширует поиск фактов.
    """
    effective_top_k = top_k or settings.retrieval_top_k
    llm = build_llm()
    rag_questions = extract_rag_questions(question, llm=llm, history=history)

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
                "history": format_history_for_prompt(history or []),
            }
        )
    )

    sources = [
        {
            "thread_id": d.metadata.get("thread_id"),
            "subject": d.metadata.get("subject"),
            "source": d.metadata.get("source"),
            "file_path": d.metadata.get("file_path"),
            "priority": d.metadata.get("priority"),
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
