"""Retrieval-augmented answer chain: question -> Qdrant top-k -> DeepSeek answer.

Built with LangChain LCEL (RunnableParallel/RunnableLambda + ChatPromptTemplate)
so it composes cleanly with the retriever in retriever.py and the DeepSeek
chat model (OpenAI-compatible endpoint) below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from .config import settings
from .retriever import MtrKnowledgeBaseRetriever

SYSTEM_PROMPT = """You are an internal assistant for MoveToRussia.com relocation managers.

You answer INTERNAL questions from managers, using only the CONTEXT below, which
consists of real precedents from past client email exchanges and an internal FAQ
catalog. This is not a conversation with a client.

Hard rules:
- Base your answer strictly on the provided CONTEXT. Do not invent facts, prices,
  timelines, legal guarantees, links, or statistics that are not present in it.
- If the context is insufficient or contradictory, say so explicitly and tell the
  manager what is missing, instead of guessing.
- When useful, mention which precedent(s) you relied on (thread_id / subject), so
  the manager can open the original thread if needed.
- Be concise and practical -- the manager needs an actionable answer, not an essay.
- Answer in the same language the manager asked in (default: Russian)."""

USER_TEMPLATE = """CONTEXT (past precedents / FAQ, most relevant first):
{context}

MANAGER QUESTION:
{question}"""


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
    answer_text = text_chain.invoke({"context": format_docs(docs), "question": question})

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
