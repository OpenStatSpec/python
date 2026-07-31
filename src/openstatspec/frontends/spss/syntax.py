"""Tokenizer, catalog-independent AST, and parser for the SPSS MVP subset."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
from typing import Literal

from ...transform.errors import SourcePosition, SourceSpan, frontend_error


TokenKind = Literal[
    "identifier", "number", "string", "left_paren", "right_paren",
    "equals", "comma", "slash", "period", "eof",
]


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    value: str | float | None
    span: SourceSpan


@dataclass(frozen=True)
class SyntaxLiteral:
    kind: Literal["numeric", "string"]
    value: float | str
    span: SourceSpan


@dataclass(frozen=True)
class RecodeMatchSyntax:
    kind: Literal["values", "range", "system_missing", "else"]
    span: SourceSpan
    values: tuple[SyntaxLiteral, ...] = ()
    lower: SyntaxLiteral | None = None
    upper: SyntaxLiteral | None = None


@dataclass(frozen=True)
class RecodeResultSyntax:
    kind: Literal["literal", "system_missing", "copy"]
    span: SourceSpan
    value: SyntaxLiteral | None = None


@dataclass(frozen=True)
class RecodeClauseSyntax:
    match: RecodeMatchSyntax
    result: RecodeResultSyntax
    span: SourceSpan


@dataclass(frozen=True)
class RecodeCommandSyntax:
    sources: tuple[Token, ...]
    clauses: tuple[RecodeClauseSyntax, ...]
    targets: tuple[Token, ...] | None
    span: SourceSpan


@dataclass(frozen=True)
class VariableLabelSyntax:
    variable: Token
    label: Token
    span: SourceSpan


@dataclass(frozen=True)
class VariableLabelsCommandSyntax:
    assignments: tuple[VariableLabelSyntax, ...]
    span: SourceSpan


@dataclass(frozen=True)
class ValueLabelSyntax:
    value: SyntaxLiteral
    label: Token
    span: SourceSpan


@dataclass(frozen=True)
class ValueLabelsGroupSyntax:
    variables: tuple[Token, ...]
    labels: tuple[ValueLabelSyntax, ...]
    span: SourceSpan


@dataclass(frozen=True)
class ValueLabelsCommandSyntax:
    groups: tuple[ValueLabelsGroupSyntax, ...]
    span: SourceSpan


SyntaxCommand = (
    RecodeCommandSyntax | VariableLabelsCommandSyntax | ValueLabelsCommandSyntax
)


@dataclass(frozen=True)
class SpssSyntaxProgram:
    commands: tuple[SyntaxCommand, ...]
    span: SourceSpan


_NUMBER = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[Ee][+-]?[0-9]+)?"
)
_IDENTIFIER_START = frozenset("_@$#")
_IDENTIFIER_CONTINUE = frozenset("_@$#")


def _position(source: str, offset: int) -> SourcePosition:
    prefix = source[:offset]
    line = prefix.count("\n") + 1
    last_newline = prefix.rfind("\n")
    column = offset + 1 if last_newline < 0 else offset - last_newline
    return SourcePosition(offset=offset, line=line, column=column)


def _span(source: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(_position(source, start), _position(source, end))


def _joined_span(first: SourceSpan, last: SourceSpan) -> SourceSpan:
    return SourceSpan(first.start, last.end)


def normalize_spss_source(source: str) -> str:
    """Normalize source line endings without changing any other source byte."""
    if not isinstance(source, str):
        raise TypeError("source must be text")
    return source.replace("\r\n", "\n").replace("\r", "\n")


def spss_source_hash(source: str) -> str:
    """Hash the exact UTF-8 source after normative LF normalization."""
    normalized = normalize_spss_source(source)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tokenize_spss(source: str) -> tuple[Token, ...]:
    """Tokenize supported SPSS text without consulting a dataset catalog."""
    if not isinstance(source, str):
        raise TypeError("source must be text")
    tokens: list[Token] = []
    offset = 0
    punctuation: dict[str, TokenKind] = {
        "(": "left_paren", ")": "right_paren", "=": "equals",
        ",": "comma", "/": "slash", ".": "period",
    }
    while offset < len(source):
        character = source[offset]
        if character.isspace():
            offset += 1
            continue
        if character in {"'", '"'}:
            start = offset
            quote = character
            offset += 1
            value: list[str] = []
            while offset < len(source):
                character = source[offset]
                if character == quote:
                    if offset + 1 < len(source) and source[offset + 1] == quote:
                        value.append(quote)
                        offset += 2
                        continue
                    offset += 1
                    tokens.append(Token(
                        "string", source[start:offset], "".join(value),
                        _span(source, start, offset),
                    ))
                    break
                value.append(character)
                offset += 1
            else:
                raise frontend_error(
                    "spss_syntax_error", "Unterminated string literal.",
                    span=_span(source, start, len(source)),
                )
            continue
        number = _NUMBER.match(source, offset)
        if number is not None:
            start, offset = offset, number.end()
            text = source[start:offset]
            try:
                value = float(text)
            except ValueError as error:  # pragma: no cover - guarded by regex
                raise frontend_error(
                    "spss_syntax_error", "Invalid numeric literal.",
                    span=_span(source, start, offset),
                ) from error
            if not math.isfinite(value):
                raise frontend_error(
                    "spss_syntax_error", "Numeric literals must be finite binary64 values.",
                    span=_span(source, start, offset),
                )
            tokens.append(Token(
                "number", text, value, _span(source, start, offset),
            ))
            continue
        if character.isalpha() or character in _IDENTIFIER_START:
            start = offset
            offset += 1
            while offset < len(source):
                candidate = source[offset]
                if candidate.isalnum() or candidate in _IDENTIFIER_CONTINUE:
                    offset += 1
                    continue
                if (
                    candidate == "." and offset + 1 < len(source)
                    and (
                        source[offset + 1].isalnum()
                        or source[offset + 1] in _IDENTIFIER_CONTINUE
                    )
                ):
                    offset += 1
                    continue
                break
            text = source[start:offset]
            tokens.append(Token(
                "identifier", text, text, _span(source, start, offset),
            ))
            continue
        if character in punctuation:
            tokens.append(Token(
                punctuation[character], character, character,
                _span(source, offset, offset + 1),
            ))
            offset += 1
            continue
        raise frontend_error(
            "spss_syntax_error", f"Unexpected character {character!r}.",
            span=_span(source, offset, offset + 1),
        )
    end = _position(source, len(source))
    tokens.append(Token("eof", "", None, SourceSpan(end, end)))
    return tuple(tokens)


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize_spss(source)
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        if token.kind != "eof":
            self.index += 1
        return token

    def accepts(self, kind: TokenKind) -> Token | None:
        if self.current.kind == kind:
            return self.advance()
        return None

    def accepts_keyword(self, keyword: str) -> Token | None:
        token = self.current
        if token.kind == "identifier" and token.text.casefold() == keyword.casefold():
            return self.advance()
        return None

    def expects(self, kind: TokenKind, detail: str) -> Token:
        token = self.accepts(kind)
        if token is None:
            raise frontend_error("spss_syntax_error", detail, span=self.current.span)
        return token

    def expects_keyword(self, keyword: str) -> Token:
        token = self.accepts_keyword(keyword)
        if token is None:
            raise frontend_error(
                "spss_syntax_error", f"Expected keyword {keyword}.",
                span=self.current.span,
            )
        return token

    def variable_list(self, *, stop_kinds: frozenset[str]) -> tuple[Token, ...]:
        variables: list[Token] = []
        while self.current.kind not in stop_kinds:
            if self.accepts("comma") is not None:
                continue
            variables.append(self.expects("identifier", "Expected a variable name."))
        if not variables:
            raise frontend_error(
                "spss_syntax_error", "Expected at least one variable name.",
                span=self.current.span,
            )
        return tuple(variables)

    def literal(self) -> SyntaxLiteral:
        token = self.current
        if token.kind == "number":
            self.advance()
            assert isinstance(token.value, float)
            return SyntaxLiteral("numeric", token.value, token.span)
        if token.kind == "string":
            self.advance()
            assert isinstance(token.value, str)
            return SyntaxLiteral("string", token.value, token.span)
        raise frontend_error(
            "spss_syntax_error", "Expected a numeric or string literal.",
            span=token.span,
        )

    def recode_result(self) -> RecodeResultSyntax:
        if (token := self.accepts_keyword("SYSMIS")) is not None:
            return RecodeResultSyntax("system_missing", token.span)
        if (token := self.accepts_keyword("COPY")) is not None:
            return RecodeResultSyntax("copy", token.span)
        literal = self.literal()
        return RecodeResultSyntax("literal", literal.span, value=literal)

    def recode_clause(self) -> RecodeClauseSyntax:
        left = self.expects("left_paren", "Expected '(' before a RECODE rule.")
        if (token := self.accepts_keyword("ELSE")) is not None:
            match = RecodeMatchSyntax("else", token.span)
        elif (token := self.accepts_keyword("SYSMIS")) is not None:
            match = RecodeMatchSyntax("system_missing", token.span)
        else:
            first = self.literal()
            if self.accepts_keyword("THRU") is not None:
                upper = self.literal()
                match = RecodeMatchSyntax(
                    "range", _joined_span(first.span, upper.span),
                    lower=first, upper=upper,
                )
            else:
                values = [first]
                while self.current.kind != "equals":
                    self.accepts("comma")
                    if self.current.kind == "equals":
                        break
                    values.append(self.literal())
                match = RecodeMatchSyntax(
                    "values", _joined_span(values[0].span, values[-1].span),
                    values=tuple(values),
                )
        self.expects("equals", "Expected '=' in a RECODE rule.")
        result = self.recode_result()
        right = self.expects("right_paren", "Expected ')' after a RECODE rule.")
        return RecodeClauseSyntax(match, result, _joined_span(left.span, right.span))

    def recode(self, start: Token) -> RecodeCommandSyntax:
        sources = self.variable_list(stop_kinds=frozenset({"left_paren", "period", "eof"}))
        clauses: list[RecodeClauseSyntax] = []
        while self.current.kind == "left_paren":
            clauses.append(self.recode_clause())
        if not clauses:
            raise frontend_error(
                "spss_syntax_error", "RECODE requires at least one rule.",
                span=self.current.span,
            )
        else_indexes = [
            index for index, clause in enumerate(clauses)
            if clause.match.kind == "else"
        ]
        if len(else_indexes) > 1:
            duplicate = clauses[else_indexes[1]]
            raise frontend_error(
                "duplicate_else", "RECODE may contain at most one ELSE rule.",
                span=duplicate.match.span,
            )
        if else_indexes and else_indexes[0] != len(clauses) - 1:
            raise frontend_error(
                "else_not_last", "ELSE must be the last RECODE rule.",
                span=clauses[else_indexes[0]].match.span,
            )
        targets = None
        if self.accepts_keyword("INTO") is not None:
            targets = self.variable_list(stop_kinds=frozenset({"period", "eof"}))
            if len(targets) != len(sources):
                raise frontend_error(
                    "spss_syntax_error",
                    "RECODE INTO requires one target for every source variable.",
                    span=_joined_span(targets[0].span, targets[-1].span),
                )
        end = self.expects("period", "Expected '.' after RECODE.")
        return RecodeCommandSyntax(
            sources, tuple(clauses), targets, _joined_span(start.span, end.span),
        )

    def variable_labels(self, start: Token) -> VariableLabelsCommandSyntax:
        self.expects_keyword("LABELS")
        assignments: list[VariableLabelSyntax] = []
        while self.current.kind not in {"period", "eof"}:
            self.accepts("slash")
            variable = self.expects("identifier", "Expected a variable name.")
            label = self.expects("string", "Expected a quoted variable label.")
            assignments.append(VariableLabelSyntax(
                variable, label, _joined_span(variable.span, label.span),
            ))
        if not assignments:
            raise frontend_error(
                "spss_syntax_error", "VARIABLE LABELS requires an assignment.",
                span=self.current.span,
            )
        end = self.expects("period", "Expected '.' after VARIABLE LABELS.")
        return VariableLabelsCommandSyntax(
            tuple(assignments), _joined_span(start.span, end.span),
        )

    def value_labels(self, start: Token) -> ValueLabelsCommandSyntax:
        self.expects_keyword("LABELS")
        groups: list[ValueLabelsGroupSyntax] = []
        while self.current.kind not in {"period", "eof"}:
            self.accepts("slash")
            group_start = self.current
            variables = self.variable_list(
                stop_kinds=frozenset({"number", "string", "period", "slash", "eof"})
            )
            labels: list[ValueLabelSyntax] = []
            while self.current.kind not in {"period", "slash", "eof"}:
                value = self.literal()
                label = self.expects("string", "Expected a quoted value label.")
                labels.append(ValueLabelSyntax(
                    value, label, _joined_span(value.span, label.span),
                ))
            if not labels:
                raise frontend_error(
                    "spss_syntax_error", "VALUE LABELS requires at least one value-label pair.",
                    span=self.current.span,
                )
            groups.append(ValueLabelsGroupSyntax(
                variables, tuple(labels),
                _joined_span(group_start.span, labels[-1].span),
            ))
        end = self.expects("period", "Expected '.' after VALUE LABELS.")
        return ValueLabelsCommandSyntax(
            tuple(groups), _joined_span(start.span, end.span),
        )

    def parse(self) -> SpssSyntaxProgram:
        commands: list[SyntaxCommand] = []
        while self.current.kind != "eof":
            start = self.expects("identifier", "Expected an SPSS command.")
            command = start.text.casefold()
            if command == "recode":
                commands.append(self.recode(start))
            elif command == "variable":
                commands.append(self.variable_labels(start))
            elif command == "value":
                commands.append(self.value_labels(start))
            else:
                raise frontend_error(
                    "unsupported_spss_command",
                    f"Unsupported SPSS command {start.text!r}.", span=start.span,
                    command=start.text,
                )
        if commands:
            program_span = _joined_span(commands[0].span, commands[-1].span)
        else:
            program_span = self.current.span
        return SpssSyntaxProgram(tuple(commands), program_span)


def parse_spss_syntax(source: str) -> SpssSyntaxProgram:
    """Parse the supported command subset into a catalog-independent AST."""
    normalized = normalize_spss_source(source)
    comment = re.search(r"(?m)^[ \t]*\*", normalized)
    if comment is not None:
        raise frontend_error(
            "unsupported_spss_command",
            "SPSS comment statements are outside the v0.1 subset.",
            span=_span(normalized, comment.start(), comment.start() + 1),
            command="*",
        )
    return _Parser(normalized).parse()
