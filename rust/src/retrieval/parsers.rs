//! Bounded-memory text and Markdown chunking for local indexing.
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};
use thiserror::Error;

/// A document chunk with optional location and heading metadata.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ParsedChunk {
    pub text: String,
    pub page_or_line: Option<String>,
    pub heading: Option<String>,
}

/// Parser limits, deliberately bounded to avoid unexpectedly large allocations.
#[derive(Clone, Copy, Debug)]
pub struct ParseOptions {
    pub chunk_chars: usize,
    pub overlap_chars: usize,
    pub max_bytes: u64,
    pub max_chars: usize,
}

impl Default for ParseOptions {
    fn default() -> Self {
        Self {
            chunk_chars: 1200,
            overlap_chars: 150,
            max_bytes: 50 * 1024 * 1024,
            max_chars: 5_000_000,
        }
    }
}

#[derive(Debug, Error)]
pub enum ParseError {
    #[error("unsupported document type: {0}")]
    Unsupported(String),
    #[error("document exceeds configured byte limit ({size} > {limit})")]
    TooLarge { size: u64, limit: u64 },
    #[error("invalid chunk limits")]
    InvalidLimits,
    #[error("document read failed: {0}")]
    Io(#[from] io::Error),
}

/// Parse UTF-8 text or Markdown in bounded chunks.
///
/// This first native parser stage handles `.txt` and `.md`; PDF and DOCX
/// adapters can share the same chunking function without changing callers.
pub fn parse_file(
    path: impl AsRef<Path>,
    options: ParseOptions,
) -> Result<Vec<ParsedChunk>, ParseError> {
    validate_options(options)?;
    let path = path.as_ref();
    let extension = path
        .extension()
        .and_then(|part| part.to_str())
        .unwrap_or_default();
    if !extension.eq_ignore_ascii_case("txt") && !extension.eq_ignore_ascii_case("md") {
        return Err(ParseError::Unsupported(extension.to_owned()));
    }
    let size = path.metadata()?.len();
    if size > options.max_bytes {
        return Err(ParseError::TooLarge {
            size,
            limit: options.max_bytes,
        });
    }
    let mut text = String::with_capacity(size.min(options.max_chars as u64) as usize);
    File::open(path)?
        .take(options.max_chars as u64)
        .read_to_string(&mut text)?;
    Ok(chunk_text(&text, options))
}

/// Split text near line boundaries while retaining a bounded overlap.
pub fn chunk_text(text: &str, options: ParseOptions) -> Vec<ParsedChunk> {
    if validate_options(options).is_err() {
        return Vec::new();
    }
    let text = text.trim();
    let mut chunks = Vec::new();
    let mut start = 0;
    let mut heading: Option<String> = None;
    while start < text.len() {
        let mut end = text.len().min(start.saturating_add(options.chunk_chars));
        if end < text.len() {
            let minimum = start + options.chunk_chars / 2;
            if let Some(boundary) = text
                .get(minimum..end)
                .and_then(|s| s.rfind('\n'))
                .map(|i| minimum + i)
            {
                end = boundary;
            }
        }
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        let piece = text[start..end].trim();
        if !piece.is_empty() {
            for line in piece.lines() {
                let line = line.trim_start();
                if line.starts_with('#') {
                    heading = Some(
                        line.trim_start_matches('#')
                            .trim()
                            .chars()
                            .take(200)
                            .collect(),
                    );
                }
            }
            chunks.push(ParsedChunk {
                text: piece.to_owned(),
                page_or_line: None,
                heading: heading.clone(),
            });
        }
        if end >= text.len() {
            break;
        }
        let next = end.saturating_sub(options.overlap_chars).max(start + 1);
        start = next;
        while !text.is_char_boundary(start) {
            start += 1;
        }
    }
    chunks
}

fn validate_options(options: ParseOptions) -> Result<(), ParseError> {
    if options.chunk_chars < 100
        || options.overlap_chars >= options.chunk_chars
        || options.max_bytes == 0
        || options.max_chars == 0
    {
        return Err(ParseError::InvalidLimits);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn chunks_text_on_line_boundaries_and_keeps_heading() {
        let input = format!("# Heading\n{}\n{}", "a".repeat(110), "b".repeat(110));
        let chunks = chunk_text(
            &input,
            ParseOptions {
                chunk_chars: 120,
                overlap_chars: 10,
                ..ParseOptions::default()
            },
        );
        assert!(chunks.len() >= 2);
        assert!(
            chunks
                .iter()
                .all(|chunk| chunk.heading.as_deref() == Some("Heading"))
        );
    }

    #[test]
    fn parses_text_and_respects_byte_limit() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("notes.TXT");
        std::fs::write(&path, "hello filebot").unwrap();
        let parsed = parse_file(&path, ParseOptions::default()).unwrap();
        assert_eq!(parsed[0].text, "hello filebot");
        let result = parse_file(
            &path,
            ParseOptions {
                max_bytes: 2,
                ..ParseOptions::default()
            },
        );
        assert!(matches!(result, Err(ParseError::TooLarge { .. })));
    }

    #[test]
    fn rejects_unsupported_types_and_bad_limits() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("x.pdf");
        std::fs::write(&path, b"not parsed yet").unwrap();
        assert!(matches!(
            parse_file(&path, ParseOptions::default()),
            Err(ParseError::Unsupported(_))
        ));
        assert!(
            chunk_text(
                "x",
                ParseOptions {
                    chunk_chars: 10,
                    ..ParseOptions::default()
                }
            )
            .is_empty()
        );
    }
}
