"""Grounded natural-language answer synthesis for Intelligent Discovery.

The spec's default response is a *catalog-grounded natural-language answer with
supporting records*, with ranked evidence available as an advanced view. This
module turns a :class:`~app.services.discovery.DiscoveryResult` into a short
answer that **points to specific records** (citations), plus the supporting
record ids.

Two paths:
- **Composed** (default, no extra infra): a deterministic, grounded summary
  built from the retrieved records — never invents facts.
- **LLM** (optional): if a Foundry *chat* deployment is configured, the same
  grounding is handed to the model for more fluent phrasing. Any failure falls
  back to the composed answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.services.semantic_embeddings import OpenAIEmbedding

if TYPE_CHECKING:
    from app.services.discovery import DiscoveryResult

_SNIPPET_CHARS = 260


@dataclass(frozen=True)
class Citation:
    """A record the answer points to."""

    label: str
    record_id: str
    source: str  # document | well | wellbore | marker | concept
    detail: str = ""


@dataclass
class DiscoveryAnswer:
    """A grounded answer plus the records it cites."""

    text: str
    citations: list[Citation] = field(default_factory=list)
    generated_by: str = "composed"  # composed | llm


def _tail(record_id: str) -> str:
    return record_id.split(":")[-1] or record_id


def _snippet(text: str, limit: int = _SNIPPET_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def build_citations(result: DiscoveryResult) -> list[Citation]:
    """Collect the records the answer should point to (deduped, ranked)."""
    citations: list[Citation] = []
    seen: set[str] = set()

    for hit in result.catalog_hits:
        if not hit.record_id or hit.record_id in seen:
            continue
        seen.add(hit.record_id)
        citations.append(
            Citation(
                label=hit.title or _tail(hit.record_id),
                record_id=hit.record_id,
                source=hit.source or "record",
                detail=_snippet(hit.content, 120),
            )
        )

    for well_id in result.anchor_well_ids:
        if well_id in seen:
            continue
        seen.add(well_id)
        citations.append(
            Citation(
                label=_tail(well_id),
                record_id=well_id,
                source="well",
                detail="anchor well",
            )
        )
    return citations


def compose_answer(result: DiscoveryResult) -> str:
    """Build a deterministic, grounded answer that references records."""
    parts: list[str] = []

    top = result.catalog_hits[0] if result.catalog_hits else None
    if top is not None:
        source_word = {
            "document": "report",
            "well": "well record",
            "wellbore": "wellbore record",
        }.get(top.source, "record")
        lead = (
            f"The strongest match is the {source_word} "
            f"**{top.title or _tail(top.record_id)}** "
            f"(`{_tail(top.record_id)}`)"
        )
        snippet = _snippet(top.content)
        if snippet and snippet.lower() not in (top.title or "").lower():
            lead += f", which notes: “{snippet}”"
        parts.append(lead + ".")

    anchors = result.anchor_well_ids
    if anchors:
        names = ", ".join(f"**{_tail(w)}**" for w in anchors[:5])
        if len(anchors) == 1:
            parts.append(f"This points to well {names}.")
        else:
            more = "" if len(anchors) <= 5 else f" (+{len(anchors) - 5} more)"
            parts.append(
                f"It resolves to {len(anchors)} anchor wells: {names}{more}."
            )

    if result.concepts:
        concept_names = ", ".join(
            f"**{c.marker_name}**" for c in result.concepts[:3]
        )
        parts.append(f"Stratigraphically it aligns with {concept_names}.")

    if not parts:
        return (
            "No records matched confidently. Try rephrasing, or broaden the "
            "question."
        )
    parts.append(
        "Open the connected graph below to see each well's related wellbores, "
        "logs, and documents, with provenance."
    )
    return " ".join(parts)


def _grounding_block(result: DiscoveryResult) -> str:
    """Compact, record-cited grounding text for the LLM."""
    lines: list[str] = []
    for hit in result.catalog_hits[:6]:
        lines.append(
            f"- [{hit.source}] {hit.title} (id={hit.record_id}): "
            f"{_snippet(hit.content, 300)}"
        )
    for concept in result.concepts[:5]:
        ages = ", ".join(concept.geological_ages) or "n/a"
        lines.append(
            f"- [concept] {concept.marker_name} (age {ages}; "
            f"{concept.wellbore_count} wellbores)"
        )
    if result.anchor_well_ids:
        anchors = ", ".join(result.anchor_well_ids)
        lines.append(f"- anchor wells: {anchors}")
    return "\n".join(lines)


def llm_answer(
    result: DiscoveryResult,
    *,
    deployment: str,
    endpoint: str,
    api_key: str,
) -> str:
    """Synthesize a grounded answer with a Foundry chat deployment."""
    from openai import OpenAI  # noqa: PLC0415 - optional dependency

    base_url = OpenAIEmbedding._azure_base_url(endpoint)
    client = OpenAI(base_url=base_url, api_key=api_key)
    grounding = _grounding_block(result)
    system = (
        "You are the ADME Intelligent Discovery assistant. Answer the user's "
        "question using ONLY the provided records. Cite records inline by their "
        "id in square brackets, e.g. [opendes:master-data--Well:AUR-01]. Be "
        "concise (3-5 sentences). If the records are insufficient, say so "
        "rather than inventing facts."
    )
    user = (
        f"Question: {result.query}\n\n"
        f"Records:\n{grounding}\n\n"
        "Write the grounded answer."
    )
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=400,
    )
    return (response.choices[0].message.content or "").strip()


def synthesize_answer(
    result: DiscoveryResult,
    *,
    chat_deployment: str | None = None,
    foundry_endpoint: str | None = None,
    foundry_key: str | None = None,
) -> DiscoveryAnswer:
    """Return a grounded answer (LLM when configured, else composed)."""
    citations = build_citations(result)
    if not result.concepts and not result.catalog_hits:
        return DiscoveryAnswer(
            text="Nothing matched. Try rephrasing the question.",
            citations=citations,
            generated_by="composed",
        )

    if chat_deployment and foundry_endpoint and foundry_key:
        try:
            text = llm_answer(
                result,
                deployment=chat_deployment,
                endpoint=foundry_endpoint,
                api_key=foundry_key,
            )
            if text:
                return DiscoveryAnswer(text, citations, "llm")
        except Exception:  # noqa: BLE001 - fall back to composed answer
            pass

    return DiscoveryAnswer(compose_answer(result), citations, "composed")
