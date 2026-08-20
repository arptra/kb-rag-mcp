"""Tree-sitter backed structural index for Java source files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_JAVA_LANGUAGE = Language(tree_sitter_java.language())
_TYPE_NODES = {
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "enum_declaration",
    "annotation_type_declaration",
}


@dataclass(frozen=True, slots=True)
class ParsedJavaField:
    name: str
    type_name: str
    annotations: str
    line: int


@dataclass(frozen=True, slots=True)
class ParsedJavaMethod:
    name: str
    annotations: str
    body: str
    line: int
    body_offset: int


@dataclass(frozen=True, slots=True)
class ParsedJavaClass:
    name: str
    package: str
    kind: str
    annotations: str
    extends: str
    fields: tuple[ParsedJavaField, ...]
    methods: tuple[ParsedJavaMethod, ...]
    line: int
    has_errors: bool


class JavaSyntaxParser:
    """Parse Java with the upstream Tree-sitter grammar without a project build."""

    def __init__(self) -> None:
        self._parser = Parser(_JAVA_LANGUAGE)

    def parse(self, path: Path, source: bytes) -> ParsedJavaClass | None:
        del path
        tree = self._parser.parse(source)
        root = tree.root_node
        package = self._package(root, source)
        declaration = next(
            (child for child in root.named_children if child.type in _TYPE_NODES),
            None,
        )
        if declaration is None:
            return None
        name_node = declaration.child_by_field_name("name")
        body = declaration.child_by_field_name("body")
        if name_node is None or body is None:
            return None
        fields = tuple(self._fields(body, source))
        methods = tuple(self._methods(body, source))
        return ParsedJavaClass(
            name=self._text(name_node, source),
            package=package,
            kind=declaration.type.removesuffix("_declaration").replace("_", " "),
            annotations=self._modifiers(declaration, source),
            extends=self._header(declaration, body, source),
            fields=fields,
            methods=methods,
            line=declaration.start_point.row + 1,
            has_errors=root.has_error,
        )

    def _fields(self, body: Node, source: bytes) -> list[ParsedJavaField]:
        fields: list[ParsedJavaField] = []
        for declaration in body.named_children:
            if declaration.type != "field_declaration":
                continue
            type_node = declaration.child_by_field_name("type")
            type_name = self._text(type_node, source) if type_node is not None else "unknown"
            annotations = self._modifiers(declaration, source)
            for child in declaration.named_children:
                if child.type != "variable_declarator":
                    continue
                name = child.child_by_field_name("name")
                if name is None:
                    continue
                fields.append(
                    ParsedJavaField(
                        name=self._text(name, source),
                        type_name=type_name,
                        annotations=annotations,
                        line=declaration.start_point.row + 1,
                    )
                )
        return fields

    def _methods(self, body: Node, source: bytes) -> list[ParsedJavaMethod]:
        methods: list[ParsedJavaMethod] = []
        for declaration in body.named_children:
            if declaration.type != "method_declaration":
                continue
            name = declaration.child_by_field_name("name")
            if name is None:
                continue
            block = declaration.child_by_field_name("body")
            if block is None:
                body_text = ""
                body_offset = declaration.end_byte
            else:
                start = min(block.end_byte, block.start_byte + 1)
                end = max(start, block.end_byte - 1)
                body_text = source[start:end].decode("utf-8", errors="replace")
                body_offset = start
            methods.append(
                ParsedJavaMethod(
                    name=self._text(name, source),
                    annotations=self._modifiers(declaration, source),
                    body=body_text,
                    line=declaration.start_point.row + 1,
                    body_offset=body_offset,
                )
            )
        return methods

    @staticmethod
    def _package(root: Node, source: bytes) -> str:
        declaration = next(
            (child for child in root.named_children if child.type == "package_declaration"),
            None,
        )
        if declaration is None:
            return ""
        name = next(
            (
                child
                for child in declaration.named_children
                if child.type in {"identifier", "scoped_identifier"}
            ),
            None,
        )
        return JavaSyntaxParser._text(name, source) if name is not None else ""

    @staticmethod
    def _modifiers(declaration: Node, source: bytes) -> str:
        modifiers = next(
            (child for child in declaration.named_children if child.type == "modifiers"),
            None,
        )
        return JavaSyntaxParser._text(modifiers, source) if modifiers is not None else ""

    @staticmethod
    def _header(declaration: Node, body: Node, source: bytes) -> str:
        return source[declaration.start_byte : body.start_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _text(node: Node, source: bytes) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
