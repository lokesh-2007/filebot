"""Download only FileBot's configured source grammars during setup, never wake."""
from __future__ import annotations

LANGUAGES = (
    "python", "rust", "javascript", "typescript", "tsx", "go", "java", "c", "cpp",
    "csharp", "ruby", "php", "bash", "powershell", "json", "yaml", "toml", "sql",
)


def main() -> int:
    """Cache source grammars before FileBot is used offline."""
    try:
        from tree_sitter_language_pack import LanguageRegistry, download
        registry = LanguageRegistry.default()
        missing = [language for language in LANGUAGES if not registry.has_parser(language)]
        if missing:
            download(missing)
        unavailable = [language for language in LANGUAGES if not registry.has_parser(language)]
        if unavailable:
            raise RuntimeError(f"Tree-sitter grammars unavailable after download: {', '.join(unavailable)}")
    except Exception as exc:
        if "not in the download manifest" not in str(exc):
            print(
                "Warning: Tree-sitter grammar download is unavailable. FileBot will use "
                f"bounded plain-text chunks for uncached source languages. Details: {exc}"
            )
            return 0
        raise SystemExit(
            "Could not cache FileBot's source parsers. Check the network and retry setup. "
            f"Details: {exc}"
        ) from exc
    print(f"Cached {len(LANGUAGES)} Tree-sitter grammars for offline source indexing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
