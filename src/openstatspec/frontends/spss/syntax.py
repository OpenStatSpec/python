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
    "equals", "less", "less_equal", "greater", "greater_equal",
    "comma", "plus", "minus", "slash", "period", "eof", "not_equal",
]


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    value: str | float | None
    span: SourceSpan


@dataclass(frozen=True)
class VariableRangeSyntax:
    first: Token
    last: Token
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
    sources: tuple[Token | VariableRangeSyntax, ...]
    clauses: tuple[RecodeClauseSyntax, ...]
    targets: tuple[Token, ...] | None
    span: SourceSpan

@dataclass(frozen=True)
class OperandSyntax:
    kind: Literal["variable", "literal"]
    span: SourceSpan
    variable: Token | None = None
    literal: SyntaxLiteral | None = None


@dataclass(frozen=True)
class ComparisonSyntax:
    left: OperandSyntax
    operator: Literal["=", "<", "<=", ">", ">=", "ne"]
    right: OperandSyntax
    span: SourceSpan


@dataclass(frozen=True)
class BooleanSyntax:
    operator: Literal["and", "or"]
    operands: tuple["PredicateSyntax", ...]
    span: SourceSpan


@dataclass(frozen=True)
class NotSyntax:
    operand: "PredicateSyntax"
    span: SourceSpan


PredicateSyntax = ComparisonSyntax | BooleanSyntax | NotSyntax


@dataclass(frozen=True)
class ComputeCommandSyntax:
    target: Token
    value: OperandSyntax
    span: SourceSpan


@dataclass(frozen=True)
class IfCommandSyntax:
    condition: PredicateSyntax
    target: Token
    value: OperandSyntax
    span: SourceSpan


@dataclass(frozen=True)
class FormatAssignmentSyntax:
    variable: Token | VariableRangeSyntax
    family: str
    width: int
    decimals: int
    span: SourceSpan


@dataclass(frozen=True)
class FormatsCommandSyntax:
    assignments: tuple[FormatAssignmentSyntax, ...]
    span: SourceSpan


@dataclass(frozen=True)
class VariableLevelAssignmentSyntax:
    variable: Token | VariableRangeSyntax
    level: Literal["nominal", "ordinal", "scale"]
    span: SourceSpan


@dataclass(frozen=True)
class VariableLevelCommandSyntax:
    assignments: tuple[VariableLevelAssignmentSyntax, ...]
    span: SourceSpan


@dataclass(frozen=True)
class ExecuteCommandSyntax:
    span: SourceSpan


@dataclass(frozen=True)
class StringCommandSyntax:
    variables: tuple[Token, ...]
    width: int
    span: SourceSpan


@dataclass(frozen=True)
class DeleteVariablesCommandSyntax:
    variables: tuple[Token, ...]
    span: SourceSpan



@dataclass(frozen=True)
class VariableLabelSyntax:
    variable: Token | VariableRangeSyntax
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
    variables: tuple[Token | VariableRangeSyntax, ...]
    labels: tuple[ValueLabelSyntax, ...]
    span: SourceSpan


@dataclass(frozen=True)
class ValueLabelsCommandSyntax:
    groups: tuple[ValueLabelsGroupSyntax, ...]
    span: SourceSpan
    additive: bool = False


SyntaxCommand = (
    RecodeCommandSyntax | ComputeCommandSyntax | IfCommandSyntax
    | VariableLabelsCommandSyntax | ValueLabelsCommandSyntax
    | FormatsCommandSyntax | VariableLevelCommandSyntax | ExecuteCommandSyntax
    | StringCommandSyntax | DeleteVariablesCommandSyntax
)


@dataclass(frozen=True)
class SpssSyntaxProgram:
    commands: tuple[SyntaxCommand, ...]
    span: SourceSpan
    official_v03: bool = False


_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?"
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


def tokenize_spss(source: str, *, official_v03: bool = False) -> tuple[Token, ...]:
    """Tokenize supported SPSS text without consulting a dataset catalog."""
    if not isinstance(source, str):
        raise TypeError("source must be text")
    tokens: list[Token] = []
    offset = 0
    punctuation: dict[str, TokenKind] = {
        "(": "left_paren", ")": "right_paren", "=": "equals",
        "<": "less", ">": "greater", ",": "comma", "+": "plus",
        "-": "minus", "/": "slash", ".": "period",
    }
    while offset < len(source):
        character = source[offset]
        if character.isspace():
            offset += 1
            continue
        if official_v03 and source.startswith("/*", offset):
            end = source.find("*/", offset + 2)
            nested = source.find("/*", offset + 2)
            if end < 0 or (nested >= 0 and nested < end):
                raise frontend_error("spss_syntax_error", "Unterminated or nested block comment.", span=_span(source, offset, len(source)))
            offset = end + 2
            continue
        boundary = not tokens or tokens[-1].kind == "period"
        if official_v03 and boundary and (
            character == "*" or re.match(r"COMMENT\b", source[offset:], re.IGNORECASE | re.ASCII)
        ):
            end = source.find(".", offset)
            if end < 0:
                raise frontend_error("spss_syntax_error", "Expected '.' after comment.", span=_span(source, offset, len(source)))
            offset = end + 1
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
            if text.casefold() in {"nan", "infinity"}:
                raise frontend_error(
                    "spss_syntax_error",
                    "NaN and Infinity are not finite-number tokens.",
                    span=_span(source, start, offset),
                )
            tokens.append(Token(
                "identifier", text, text, _span(source, start, offset),
            ))
            continue
        if official_v03 and source[offset:offset + 2] in {"<>", "~="}:
            text = source[offset:offset + 2]
            tokens.append(Token("not_equal", text, text, _span(source, offset, offset + 2)))
            offset += 2
            continue
        if source.startswith("<=", offset) or source.startswith(">=", offset):
            text = source[offset:offset + 2]
            tokens.append(Token(
                "less_equal" if text == "<=" else "greater_equal",
                text, text, _span(source, offset, offset + 2),
            ))
            offset += 2
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
    def __init__(self, source: str, *, official_v03: bool = False) -> None:
        self.source = source
        self.official_v03 = official_v03
        self.tokens = tokenize_spss(source, official_v03=official_v03)
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
        if (token.kind == "identifier" and token.text.casefold() == keyword.casefold()
                and (not self.official_v03 or token.text.isascii())):
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

    def variable_list(self, *, stop_kinds: frozenset[str]) -> tuple[Token | VariableRangeSyntax, ...]:
        variables: list[Token | VariableRangeSyntax] = []
        while self.current.kind not in stop_kinds:
            if self.accepts("comma") is not None:
                continue
            first = self.expects("identifier", "Expected a variable name.")
            if self.official_v03 and self.accepts_keyword("TO") is not None:
                last = self.expects("identifier", "Expected a TO endpoint.")
                variables.append(VariableRangeSyntax(first, last, _joined_span(first.span, last.span)))
            else:
                variables.append(first)
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

    def operand(self) -> OperandSyntax:
        if self.current.kind == "identifier":
            token = self.advance()
            return OperandSyntax("variable", token.span, variable=token)
        literal = self.literal()
        return OperandSyntax("literal", literal.span, literal=literal)

    def comparison(self) -> PredicateSyntax:
        if self.accepts("left_paren") is not None:
            expression = self.predicate()
            self.expects("right_paren", "Expected ')' after expression.")
            return expression
        left = self.operand()
        operators = {
            "equals": "=", "less": "<", "less_equal": "<=",
            "greater": ">", "greater_equal": ">=",
        }
        token = self.current
        if self.official_v03:
            operators["not_equal"] = "ne"
            if token.kind == "identifier" and token.text.isascii() and token.text.casefold() == "ne":
                operators["identifier"] = "ne"
        if token.kind not in operators:
            raise frontend_error(
                "expression_type_unsupported" if self.official_v03 and token.kind == "right_paren" else "spss_syntax_error", "Expected a comparison operator.",
                span=token.span,
            )
        self.advance()
        right = self.operand()
        return ComparisonSyntax(
            left, operators[token.kind], right, _joined_span(left.span, right.span),
        )

    def negation(self) -> PredicateSyntax:
        if self.official_v03 and (token := self.accepts_keyword("NOT")) is not None:
            operand = self.negation()
            return NotSyntax(operand, _joined_span(token.span, operand.span))
        return self.comparison()

    def conjunction(self) -> PredicateSyntax:
        operands = [self.negation()]
        while self.accepts_keyword("AND") is not None:
            operands.append(self.negation())
        if len(operands) == 1:
            return operands[0]
        return BooleanSyntax(
            "and", tuple(operands), _joined_span(operands[0].span, operands[-1].span),
        )

    def predicate(self) -> PredicateSyntax:
        operands = [self.conjunction()]
        while self.accepts_keyword("OR") is not None:
            operands.append(self.conjunction())
        if len(operands) == 1:
            return operands[0]
        return BooleanSyntax(
            "or", tuple(operands), _joined_span(operands[0].span, operands[-1].span),
        )

    def compute(self, start: Token) -> ComputeCommandSyntax:
        target = self.expects("identifier", "COMPUTE requires a target variable.")
        self.expects("equals", "Expected '=' in COMPUTE.")
        value = self.operand()
        if (
            self.current.kind in {"plus", "minus", "slash"}
            or (
                self.current.kind == "number"
                and self.current.text.startswith("-")
            )
        ):
            raise frontend_error(
                "expression_type_unsupported",
                "Arithmetic expressions are outside the bounded frontend.",
                span=self.current.span,
            )
        end = self.expects("period", "Expected '.' after COMPUTE.")
        return ComputeCommandSyntax(target, value, _joined_span(start.span, end.span))

    def if_command(self, start: Token) -> IfCommandSyntax:
        self.expects("left_paren", "Expected '(' before the IF predicate.")
        condition = self.predicate()
        self.expects("right_paren", "Expected ')' after the IF predicate.")
        target = self.expects("identifier", "IF requires a target variable.")
        self.expects("equals", "Expected '=' in IF assignment.")
        value = self.operand()
        end = self.expects("period", "Expected '.' after IF.")
        return IfCommandSyntax(condition, target, value, _joined_span(start.span, end.span))

    def formats(self, start: Token) -> FormatsCommandSyntax:
        assignments: list[FormatAssignmentSyntax] = []
        while self.current.kind not in {"period", "eof"}:
            self.accepts("slash")
            variables = (
                self.variable_list(stop_kinds=frozenset({"left_paren", "period", "eof", "slash"}))
                if self.official_v03 else
                (self.expects("identifier", "FORMATS requires a variable name."),)
            )
            self.expects("left_paren", "Expected '(' before an SPSS format.")
            format_token = self.expects("identifier", "Expected an SPSS format such as F1.0.")
            match = re.fullmatch(r"([A-Za-z]+)([0-9]+)(?:[.]([0-9]+))?", format_token.text)
            if match is None:
                raise frontend_error("invalid_format", "Expected a bounded SPSS format such as F1.0.", span=format_token.span)
            right = self.expects("right_paren", "Expected ')' after an SPSS format.")
            family, width = match.group(1).upper(), int(match.group(2))
            decimals = int(match.group(3) or 0)
            if (family != "F" or width < 1 or width > 40 or decimals > 16
                    or (decimals != 0 and width < decimals + 2)):
                raise frontend_error("invalid_format", "Only valid numeric F formats are supported.", span=format_token.span, format=format_token.text)
            assignments.extend(FormatAssignmentSyntax(variable, family, width, decimals, _joined_span(variable.span, right.span)) for variable in variables)
        if not assignments:
            raise frontend_error("spss_syntax_error", "FORMATS requires an assignment.", span=self.current.span)
        end = self.expects("period", "Expected '.' after FORMATS.")
        return FormatsCommandSyntax(tuple(assignments), _joined_span(start.span, end.span))

    def variable_level(self, start: Token) -> VariableLevelCommandSyntax:
        self.expects_keyword("LEVEL")
        assignments: list[VariableLevelAssignmentSyntax] = []
        while self.current.kind not in {"period", "eof"}:
            self.accepts("slash")
            variables = self.variable_list(
                stop_kinds=frozenset({"left_paren", "period", "eof", "slash"}),
            )
            self.expects("left_paren", "Expected '(' before a measurement level.")
            level = self.expects("identifier", "Expected NOMINAL, ORDINAL, or SCALE.")
            normalized = level.text.casefold()
            if normalized not in {"nominal", "ordinal", "scale"} or (self.official_v03 and not level.text.isascii()):
                raise frontend_error(
                    "spss_syntax_error",
                    "Expected NOMINAL, ORDINAL, or SCALE.",
                    span=level.span,
                    level=level.text,
                )
            right = self.expects(
                "right_paren", "Expected ')' after a measurement level.",
            )
            assignments.extend(
                VariableLevelAssignmentSyntax(
                    variable,
                    normalized,
                    _joined_span(variable.span, right.span),
                )
                for variable in variables
            )
        if not assignments:
            raise frontend_error(
                "spss_syntax_error",
                "VARIABLE LEVEL requires an assignment.",
                span=self.current.span,
            )
        end = self.expects("period", "Expected '.' after VARIABLE LEVEL.")
        return VariableLevelCommandSyntax(
            tuple(assignments), _joined_span(start.span, end.span),
        )

    def execute(self, start: Token) -> ExecuteCommandSyntax:
        end = self.expects("period", "Expected '.' after EXECUTE.")
        return ExecuteCommandSyntax(_joined_span(start.span, end.span))

    @staticmethod
    def reject_to_range(variables: tuple[Token, ...], command: str) -> None:
        range_token = next(
            (variable for variable in variables if variable.text.casefold() == "to"),
            None,
        )
        if range_token is not None:
            raise frontend_error(
                "unsupported_spss_feature",
                f"{command} variable ranges using TO are not supported.",
                span=range_token.span,
            )

    def string(self, start: Token) -> StringCommandSyntax:
        variables = self.variable_list(
            stop_kinds=frozenset({"left_paren", "period", "eof"}),
        )
        self.reject_to_range(variables, "STRING")
        self.expects("left_paren", "Expected '(' before a STRING width.")
        width_token = self.expects(
            "identifier", "STRING requires a width such as A20.",
        )
        match = re.fullmatch(r"A([0-9]+)", width_token.text, re.IGNORECASE)
        if match is None:
            raise frontend_error(
                "spss_syntax_error",
                "STRING supports only character widths such as A20.",
                span=width_token.span,
            )
        width = int(match.group(1))
        if not 1 <= width <= 32767:
            raise frontend_error(
                "invalid_string_width",
                "STRING width must be between 1 and 32767.",
                span=width_token.span,
                width=width,
            )
        self.expects("right_paren", "Expected ')' after a STRING width.")
        end = self.expects("period", "Expected '.' after STRING.")
        return StringCommandSyntax(
            variables, width, _joined_span(start.span, end.span),
        )

    def delete_variables(self, start: Token) -> DeleteVariablesCommandSyntax:
        self.expects_keyword("VARIABLES")
        variables = self.variable_list(stop_kinds=frozenset({"period", "eof"}))
        self.reject_to_range(variables, "DELETE VARIABLES")
        end = self.expects("period", "Expected '.' after DELETE VARIABLES.")
        return DeleteVariablesCommandSyntax(
            variables, _joined_span(start.span, end.span),
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
            lowest = self.accepts_keyword("LOWEST") if self.official_v03 else None
            first = SyntaxLiteral("numeric", -float.fromhex("0x1.fffffffffffffp+1023"), lowest.span) if lowest else self.literal()
            if self.accepts_keyword("THRU") is not None:
                highest = self.accepts_keyword("HIGHEST") if self.official_v03 else None
                upper = SyntaxLiteral("numeric", float.fromhex("0x1.fffffffffffffp+1023"), highest.span) if highest else self.literal()
                match = RecodeMatchSyntax(
                    "range", _joined_span(first.span, upper.span),
                    lower=first, upper=upper,
                )
            else:
                if lowest:
                    raise frontend_error("spss_syntax_error", "LOWEST requires THRU.", span=first.span)
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
            targets = self.variable_list(stop_kinds=frozenset({"period", "eof", "slash"}) if self.official_v03 else frozenset({"period", "eof"}))
            if self.official_v03 and any(isinstance(target, VariableRangeSyntax) or target.text.casefold() == "to" for target in targets):
                raise frontend_error("spss_syntax_error", "INTO targets cannot use TO.", span=targets[0].span)
            if not self.official_v03 and len(targets) != len(sources):
                raise frontend_error(
                    "spss_syntax_error",
                    "RECODE INTO requires one target for every source variable.",
                    span=_joined_span(targets[0].span, targets[-1].span),
                )
        end = self.current if self.official_v03 and self.current.kind == "slash" else self.expects("period", "Expected '.' after RECODE.")
        return RecodeCommandSyntax(
            sources, tuple(clauses), targets, _joined_span(start.span, end.span),
        )

    def variable_labels(self, start: Token) -> VariableLabelsCommandSyntax:
        self.expects_keyword("LABELS")
        assignments: list[VariableLabelSyntax] = []
        while self.current.kind not in {"period", "eof"}:
            self.accepts("slash")
            variables = (
                self.variable_list(stop_kinds=frozenset({"string", "period", "eof", "slash"}))
                if self.official_v03 else
                (self.expects("identifier", "Expected a variable name."),)
            )
            label = self.expects("string", "Expected a quoted variable label.")
            assignments.extend(VariableLabelSyntax(
                variable, label, _joined_span(variable.span, label.span),
            ) for variable in variables)
        if not assignments:
            raise frontend_error(
                "spss_syntax_error", "VARIABLE LABELS requires an assignment.",
                span=self.current.span,
            )
        end = self.expects("period", "Expected '.' after VARIABLE LABELS.")
        return VariableLabelsCommandSyntax(
            tuple(assignments), _joined_span(start.span, end.span),
        )

    def value_labels(self, start: Token, *, additive: bool = False) -> ValueLabelsCommandSyntax:
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
        if self.official_v03 and not groups:
            raise frontend_error("spss_syntax_error", "VALUE LABELS requires a group.", span=self.current.span)
        end = self.expects("period", "Expected '.' after VALUE LABELS.")
        return ValueLabelsCommandSyntax(
            tuple(groups), _joined_span(start.span, end.span), additive,
        )

    def parse(self) -> SpssSyntaxProgram:
        commands: list[SyntaxCommand] = []
        while self.current.kind != "eof":
            start = self.expects("identifier", "Expected an SPSS command.")
            command = start.text.casefold() if not self.official_v03 or start.text.isascii() else start.text
            if self.official_v03 and command in {"string", "delete"}:
                raise frontend_error("unsupported_spss_command", "Python schema commands are outside official Frontend 0.3.", span=start.span, command=start.text)
            if command == "recode":
                commands.append(self.recode(start))
                while self.official_v03 and self.accepts("slash") is not None:
                    commands.append(self.recode(self.current))
            elif command == "compute":
                commands.append(self.compute(start))
            elif command == "if":
                commands.append(self.if_command(start))
            elif command == "formats":
                commands.append(self.formats(start))
            elif command == "execute":
                commands.append(self.execute(start))
            elif command == "string":
                commands.append(self.string(start))
            elif command == "delete":
                commands.append(self.delete_variables(start))
            elif command == "variable":
                if self.current.kind == "identifier" and self.current.text.casefold() == "level":
                    commands.append(self.variable_level(start))
                else:
                    commands.append(self.variable_labels(start))
            elif command == "value":
                commands.append(self.value_labels(start))
            elif self.official_v03 and command == "add":
                self.expects_keyword("VALUE")
                commands.append(self.value_labels(start, additive=True))
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
        return SpssSyntaxProgram(tuple(commands), program_span, self.official_v03)


def parse_spss_syntax(source: str, *, official_v03: bool = False) -> SpssSyntaxProgram:
    """Parse the supported command subset into a catalog-independent AST."""
    normalized = normalize_spss_source(source)
    comment = re.search(r"(?m)^[ \t]*\*", normalized)
    if comment is not None and not official_v03:
        raise frontend_error(
            "unsupported_spss_command",
            "SPSS comment statements are outside the v0.1 subset.",
            span=_span(normalized, comment.start(), comment.start() + 1),
            command="*",
        )
    return _Parser(normalized, official_v03=official_v03).parse()
