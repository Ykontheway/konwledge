"""Heading-aware, offset-preserving text splitting."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from konwledge.domain import Document, DocumentContentType, KnowledgeUnit

IdFactory = Callable[[], UUID]

_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+|$)(.*?)[ \t]*$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")


@dataclass(frozen=True, slots=True)
class _Section:
    start: int
    end: int
    heading: str | None
    section_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Heading:
    start: int
    title: str
    section_path: tuple[str, ...]


class TraceableTextSplitter:
    """Split normalized text while preserving exact half-open character ranges."""

    def __init__(self, *, id_factory: IdFactory = uuid4) -> None:
        self._id_factory = id_factory

    def split(self, document: Document, content: str) -> tuple[KnowledgeUnit, ...]:
        if document.content_type is DocumentContentType.MARKDOWN:
            sections = self._markdown_sections(content)
        else:
            sections = self._paragraph_sections(content)

        return tuple(
            KnowledgeUnit(
                id=self._id_factory(),
                document_id=document.id,
                ordinal=ordinal,
                text=content[section.start : section.end],
                start_char=section.start,
                end_char=section.end,
                heading=section.heading,
                section_path=section.section_path,
            )
            for ordinal, section in enumerate(sections)
        )

    def _markdown_sections(self, content: str) -> Sequence[_Section]:
        headings = self._find_headings(content)
        if not headings:
            return self._paragraph_sections(content)

        sections: list[_Section] = []
        first_heading = headings[0]
        prefix_has_content = bool(content[: first_heading.start].strip())
        if prefix_has_content:
            sections.append(_Section(0, first_heading.start, None, ()))

        for index, heading in enumerate(headings):
            start = 0 if index == 0 and not prefix_has_content else heading.start
            end = headings[index + 1].start if index + 1 < len(headings) else len(content)
            sections.append(_Section(start, end, heading.title, heading.section_path))
        return sections

    def _find_headings(self, content: str) -> list[_Heading]:
        headings: list[_Heading] = []
        stack: list[tuple[int, str]] = []
        fence: tuple[str, int] | None = None
        offset = 0

        for line in content.splitlines(keepends=True):
            body = line.removesuffix("\n")
            fence_match = _FENCE.match(body)
            if fence_match is not None:
                marker = fence_match.group(1)
                if fence is None:
                    fence = (marker[0], len(marker))
                elif marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
                offset += len(line)
                continue

            if fence is None:
                heading_match = _HEADING.match(body)
                if heading_match is not None:
                    level = len(heading_match.group(1))
                    title = _CLOSING_HASHES.sub("", heading_match.group(2)).strip()
                    if title:
                        while stack and stack[-1][0] >= level:
                            stack.pop()
                        stack.append((level, title))
                        headings.append(_Heading(offset, title, tuple(item[1] for item in stack)))
            offset += len(line)

        return headings

    def _paragraph_sections(self, content: str) -> Sequence[_Section]:
        sections: list[_Section] = []
        start = 0
        for paragraph_break in _PARAGRAPH_BREAK.finditer(content):
            end = paragraph_break.end()
            if content[start:end].strip():
                sections.append(_Section(start, end, None, ()))
                start = end
        if content[start:].strip():
            sections.append(_Section(start, len(content), None, ()))
        return sections
