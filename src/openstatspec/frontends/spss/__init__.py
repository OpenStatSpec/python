"""SPSS syntax frontend for canonical OpenStatSpec transformation plans."""

from .binding import bind_spss_syntax
from .compiler import SpssFrontendCompilation, compile_spss_syntax
from .syntax import (
    SpssSyntaxProgram,
    normalize_spss_source,
    parse_spss_syntax,
    spss_source_hash,
    tokenize_spss,
)


SPSS_FRONTEND_CONTRACT = "openstatspec-spss-syntax-frontend-v0.1"

__all__ = [
    "SPSS_FRONTEND_CONTRACT",
    "SpssFrontendCompilation",
    "SpssSyntaxProgram",
    "bind_spss_syntax",
    "compile_spss_syntax",
    "normalize_spss_source",
    "parse_spss_syntax",
    "spss_source_hash",
    "tokenize_spss",
]
