"""Tree-sitter backed structural index for Kotlin source files."""

from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser

from gigacode_graph.java_syntax import ParsedJavaClass, ParsedJavaField, ParsedJavaMethod

_KOTLIN_LANGUAGE = Language(tree_sitter_kotlin.language())
_TYPE_NODES = {"class_declaration", "object_declaration"}


class KotlinSyntaxParser:
    """Parse Kotlin source into the scanner's language-neutral structural model."""

    def __init__(self) -> None:
        self._parser = Parser(_KOTLIN_LANGUAGE)

    def parse(self, path: Path, source: bytes) -> ParsedJavaClass | None:
        del path
        tree = self._parser.parse(source)
        root = tree.root_node
        declaration = next(
            (child for child in root.named_children if child.type in _TYPE_NODES),
            None,
        )
        if declaration is None:
            return None
        name_node = declaration.child_by_field_name("name")
        body = next(
            (child for child in declaration.named_children if child.type == "class_body"),
            None,
        )
        if name_node is None or body is None:
            return None
        text = source.decode("utf-8", errors="replace")
        name = self._text(name_node, source)
        return ParsedJavaClass(
            name=name,
            package=self._package(text),
            kind=self._kind(declaration, source),
            annotations=self._annotations(declaration, source),
            extends=source[declaration.start_byte : body.start_byte].decode(
                "utf-8", errors="replace"
            ),
            fields=tuple(self._fields(declaration, body, source)),
            methods=tuple(self._methods(name, body, source)),
            line=declaration.start_point.row + 1,
            has_errors=root.has_error,
        )

    def _fields(self, declaration: Node, body: Node, source: bytes) -> list[ParsedJavaField]:
        fields: list[ParsedJavaField] = []
        constructor = next(
            (child for child in declaration.named_children if child.type == "primary_constructor"),
            None,
        )
        containers = [body]
        if constructor is not None:
            containers.append(constructor)
        for container in containers:
            for node in self._walk(container):
                if node.type not in {"class_parameter", "property_declaration"}:
                    continue
                raw = self._text(node, source)
                match = re.search(
                    r"(?s)(?P<annotations>(?:@[\w.]+(?:\([^)]*\))?\s*)*)"
                    r"(?:public|private|protected|internal|lateinit|override|open|final|const|\s)*"
                    r"\b(?:val|var)\s+(?P<name>[A-Za-z_]\w*)\s*:\s*"
                    r"(?P<type>[A-Za-z_][\w.<>?, ]*)",
                    raw,
                )
                if match is None:
                    continue
                fields.append(
                    ParsedJavaField(
                        name=match.group("name"),
                        type_name=match.group("type").strip(),
                        annotations=match.group("annotations").strip(),
                        line=node.start_point.row + 1,
                    )
                )
        return fields

    def _methods(self, class_name: str, body: Node, source: bytes) -> list[ParsedJavaMethod]:
        del class_name
        methods: list[ParsedJavaMethod] = []
        for declaration in body.named_children:
            if declaration.type != "function_declaration":
                continue
            name = declaration.child_by_field_name("name")
            if name is None:
                continue
            function_body = next(
                (child for child in declaration.named_children if child.type == "function_body"),
                None,
            )
            if function_body is None:
                body_text = ""
                body_offset = declaration.end_byte
            else:
                raw = self._text(function_body, source)
                body_text = raw[1:-1] if raw.startswith("{") and raw.endswith("}") else raw
                body_offset = function_body.start_byte + (1 if raw.startswith("{") else 0)
            methods.append(
                ParsedJavaMethod(
                    name=self._text(name, source),
                    annotations=self._annotations(declaration, source),
                    body=body_text,
                    line=declaration.start_point.row + 1,
                    body_offset=body_offset,
                )
            )
        return methods

    def _annotations(self, declaration: Node, source: bytes) -> str:
        modifiers = next(
            (child for child in declaration.named_children if child.type == "modifiers"),
            None,
        )
        attached = self._text(modifiers, source) if modifiers is not None else ""
        # Some versions of the Kotlin grammar expose consecutive annotations immediately before
        # a declaration as annotated_expression siblings. Preserve them deterministically.
        prefix = source[max(0, declaration.start_byte - 4096) : declaration.start_byte].decode(
            "utf-8", errors="replace"
        )
        leading = re.search(
            r"(?ms)((?:^[ \t]*@[\w.]+(?:\([^\n]*\))?[ \t]*\n)+)[ \t]*$",
            prefix,
        )
        values = [
            value.strip()
            for value in (leading.group(1) if leading else "", attached)
            if value.strip()
        ]
        return "\n".join(dict.fromkeys(values))

    @staticmethod
    def _package(text: str) -> str:
        match = re.search(r"(?m)^\s*package\s+([\w.]+)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _kind(declaration: Node, source: bytes) -> str:
        header = source[declaration.start_byte : declaration.end_byte].decode(
            "utf-8", errors="replace"
        )[:200]
        if re.search(r"\binterface\s+", header):
            return "interface"
        return "object" if declaration.type == "object_declaration" else "class"

    @staticmethod
    def _walk(root: Node) -> list[Node]:
        result: list[Node] = []
        stack = list(reversed(root.named_children))
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(reversed(node.named_children))
        return result

    @staticmethod
    def _text(node: Node, source: bytes) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
