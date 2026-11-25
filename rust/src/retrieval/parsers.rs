//! Bounded-memory text and Markdown chunking for local indexing.
use quick_xml::events::Event;
use std::{
    fs::File,
    io::{self, Read, Write},
    path::Path,
};
use thiserror::Error;
use zip::ZipArchive;

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
    pub max_docx_uncompressed_bytes: u64,
}

impl Default for ParseOptions {
    fn default() -> Self {
        Self {
            chunk_chars: 1200,
            overlap_chars: 150,
            max_bytes: 50 * 1024 * 1024,
            max_chars: 5_000_000,
            max_docx_uncompressed_bytes: 100_000_000,
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
    #[error("document parser failed: {0}")]
    Parser(String),
}

/// Parse text, Markdown, PDF, or DOCX while enforcing source and output limits.
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
    let size = path.metadata()?.len();
    if size > options.max_bytes {
        return Err(ParseError::TooLarge {
            size,
            limit: options.max_bytes,
        });
    }
    if extension.eq_ignore_ascii_case("txt") || extension.eq_ignore_ascii_case("md") {
        let byte_limit = (options.max_chars as u64)
            .saturating_mul(4)
            .saturating_add(4);
        let mut bytes = Vec::with_capacity(size.min(byte_limit) as usize);
        File::open(path)?.take(byte_limit).read_to_end(&mut bytes)?;
        let text: String = String::from_utf8_lossy(&bytes)
            .chars()
            .take(options.max_chars)
            .collect();
        return Ok(chunk_text(&text, options));
    }
    if extension.eq_ignore_ascii_case("docx") {
        return parse_docx(path, options);
    }
    if extension.eq_ignore_ascii_case("pdf") {
        return parse_pdf(path, options);
    }
    Err(ParseError::Unsupported(extension.to_owned()))
}

fn parse_docx(path: &Path, options: ParseOptions) -> Result<Vec<ParsedChunk>, ParseError> {
    let file = File::open(path)?;
    let mut archive = ZipArchive::new(file)
        .map_err(|error| ParseError::Parser(format!("invalid DOCX archive: {error}")))?;
    if archive.len() > 10_000 {
        return Err(ParseError::Parser(
            "DOCX contains too many archive entries".into(),
        ));
    }
    let mut total_uncompressed = 0_u64;
    for index in 0..archive.len() {
        let entry = archive
            .by_index(index)
            .map_err(|error| ParseError::Parser(error.to_string()))?;
        total_uncompressed = total_uncompressed.saturating_add(entry.size());
        if total_uncompressed > options.max_docx_uncompressed_bytes {
            return Err(ParseError::Parser(
                "DOCX exceeds uncompressed-size limit".into(),
            ));
        }
    }
    let mut xml = String::new();
    archive
        .by_name("word/document.xml")
        .map_err(|error| ParseError::Parser(format!("DOCX is missing word/document.xml: {error}")))?
        .take(options.max_docx_uncompressed_bytes.saturating_add(1))
        .read_to_string(&mut xml)?;
    let mut reader = quick_xml::Reader::from_str(&xml);
    reader.config_mut().trim_text(true);
    let mut in_paragraph = false;
    let mut paragraph = String::new();
    let mut heading: Option<String> = None;
    let mut out = Vec::new();
    let mut remaining = options.max_chars;
    loop {
        match reader
            .read_event()
            .map_err(|error| ParseError::Parser(error.to_string()))?
        {
            Event::Start(event) if event.local_name().as_ref() == b"p" => {
                in_paragraph = true;
                paragraph.clear();
                heading = None;
            }
            Event::Empty(event) if in_paragraph && event.local_name().as_ref() == b"pStyle" => {
                for attribute in event.attributes().flatten() {
                    if attribute.key.local_name().as_ref() == b"val" {
                        let value = attribute
                            .unescape_value()
                            .map_err(|error| ParseError::Parser(error.to_string()))?;
                        if value.starts_with("Heading") {
                            heading = Some(value.to_string());
                        }
                    }
                }
            }
            Event::Text(event) if in_paragraph && remaining > 0 => {
                let decoded = event
                    .decode()
                    .map_err(|error| ParseError::Parser(error.to_string()))?;
                let decoded = quick_xml::escape::unescape(&decoded)
                    .map_err(|error| ParseError::Parser(error.to_string()))?;
                append_bounded(&mut paragraph, &decoded, remaining);
                remaining = remaining.saturating_sub(decoded.chars().count());
            }
            Event::Empty(event) if in_paragraph && event.local_name().as_ref() == b"tab" => {
                paragraph.push('\t')
            }
            Event::End(event) if event.local_name().as_ref() == b"p" => {
                let text = paragraph.trim();
                if !text.is_empty() {
                    let mut chunks = chunk_text(text, options);
                    for chunk in &mut chunks {
                        chunk.heading = heading.clone();
                    }
                    out.extend(chunks);
                }
                in_paragraph = false;
            }
            Event::Eof => break,
            _ => {}
        }
        if remaining == 0 {
            break;
        }
    }
    Ok(out)
}

fn parse_pdf(path: &Path, options: ParseOptions) -> Result<Vec<ParsedChunk>, ParseError> {
    let document =
        pdf_extract::Document::load(path).map_err(|error| ParseError::Parser(error.to_string()))?;
    let mut remaining = options.max_chars;
    let mut chunks = Vec::new();
    for page_number in document.get_pages().keys().copied() {
        if remaining == 0 {
            break;
        }
        let mut bounded = BoundedText {
            text: String::new(),
            max_chars: remaining,
        };
        {
            let mut output = pdf_extract::PlainTextOutput::new(&mut bounded as &mut dyn Write);
            pdf_extract::output_doc_page(&document, &mut output, page_number)
                .map_err(|error| ParseError::Parser(error.to_string()))?;
        }
        let text = bounded.text;
        let consumed = text.chars().count();
        for mut chunk in chunk_text(&text, options) {
            chunk.page_or_line = Some(format!("page {page_number}"));
            chunks.push(chunk);
        }
        remaining = remaining.saturating_sub(consumed);
    }
    Ok(chunks)
}

fn append_bounded(target: &mut String, source: &str, remaining_chars: usize) {
    let take_bytes = source
        .char_indices()
        .nth(remaining_chars)
        .map_or(source.len(), |(index, _)| index);
    target.push_str(&source[..take_bytes]);
}

struct BoundedText {
    text: String,
    max_chars: usize,
}
impl Write for BoundedText {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let available = self.max_chars.saturating_sub(self.text.chars().count());
        let valid_bytes =
            std::str::from_utf8(bytes).map_or_else(|error| error.valid_up_to(), |_| bytes.len());
        let valid = std::str::from_utf8(&bytes[..valid_bytes]).unwrap_or_default();
        let take_bytes = valid
            .char_indices()
            .nth(available)
            .map_or(valid.len(), |(index, _)| index);
        self.text.push_str(&valid[..take_bytes]);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
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
        || options.max_docx_uncompressed_bytes == 0
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
            Err(ParseError::Parser(_))
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

    #[test]
    fn parses_docx_paragraphs_and_heading_style() {
        use std::io::{Cursor, Write};
        let dir = tempdir().unwrap();
        let path = dir.path().join("notes.docx");
        let cursor = Cursor::new(Vec::new());
        let mut archive = zip::ZipWriter::new(cursor);
        archive
            .start_file(
                "word/document.xml",
                zip::write::SimpleFileOptions::default(),
            )
            .unwrap();
        archive.write_all(br#"<w:document xmlns:w="urn:test"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Native Rust notes</w:t></w:r></w:p></w:body></w:document>"#).unwrap();
        std::fs::write(&path, archive.finish().unwrap().into_inner()).unwrap();
        let chunks = parse_file(&path, ParseOptions::default()).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].text, "Native Rust notes");
        assert_eq!(chunks[0].heading.as_deref(), Some("Heading1"));
    }

    #[test]
    fn pdf_parser_reports_malformed_input() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("bad.pdf");
        std::fs::write(&path, b"not a PDF").unwrap();
        assert!(matches!(
            parse_file(&path, ParseOptions::default()),
            Err(ParseError::Parser(_))
        ));
    }

    #[test]
    fn extracts_pdf_text_with_page_metadata_and_limit() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("sample.pdf");
        let stream = "BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET";
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_owned(),
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".to_owned(),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_owned(),
            format!("<< /Length {} >>\nstream\n{}\nendstream", stream.len(), stream),
        ];
        let mut pdf = b"%PDF-1.4\n".to_vec();
        let mut offsets = Vec::new();
        for (index, object) in objects.iter().enumerate() {
            offsets.push(pdf.len());
            pdf.extend_from_slice(format!("{} 0 obj\n{}\nendobj\n", index + 1, object).as_bytes());
        }
        let xref = pdf.len();
        pdf.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
        );
        for offset in offsets {
            pdf.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        pdf.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF",
                objects.len() + 1
            )
            .as_bytes(),
        );
        std::fs::write(&path, pdf).unwrap();
        let parsed = parse_file(&path, ParseOptions::default()).unwrap();
        assert!(parsed.iter().any(|chunk| chunk.text.contains("Hello PDF")));
        assert!(
            parsed
                .iter()
                .all(|chunk| chunk.page_or_line.as_deref() == Some("page 1"))
        );
    }
}
