//! Wake-only ONNX embeddings fused with persistent SQLite FTS5 using RRF.
use crate::{
    config::{ConfigError, Settings, expand_path},
    storage::{Database, SearchHit, StorageError},
};
use fastembed::{EmbeddingModel, TextEmbedding, TextInitOptions};
use std::{collections::HashMap, str::FromStr, time::Instant};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum RetrievalError {
    #[error("invalid retrieval settings: {0}")]
    Config(#[from] ConfigError),
    #[error("SQLite retrieval failed: {0}")]
    Storage(#[from] StorageError),
    #[error("embedding runtime failed: {0}")]
    Embedding(#[from] fastembed::Error),
    #[error("embedding cache operation failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("unsupported embedding model `{0}`")]
    Model(String),
}

/// Retrieval interface; construction does not initialize ONNX or load weights.
pub struct HybridEngine {
    db: Database,
    model_name: EmbeddingModel,
    model_label: String,
    cache_dir: std::path::PathBuf,
    batch_size: usize,
    embedder: Option<TextEmbedding>,
}

impl HybridEngine {
    /// Build a dormant engine from validated settings without loading the model.
    pub fn new(settings: &Settings, db: Database) -> Result<Self, RetrievalError> {
        db.seed_glossary_entries(&settings.retrieval.glossary)?;
        let model_label = settings.models.embeddings.model.trim().to_owned();
        let model_name = resolve_model(&model_label)?;
        Ok(Self {
            db,
            model_name,
            model_label,
            cache_dir: expand_path(&settings.models.embeddings.cache_dir)?,
            batch_size: settings.models.embeddings.batch_size.clamp(1, 256),
            embedder: None,
        })
    }

    /// Load ONNX and local model weights after an explicit wake.
    pub fn activate(&mut self) -> Result<(), RetrievalError> {
        if self.embedder.is_some() {
            return Ok(());
        }
        std::fs::create_dir_all(&self.cache_dir)?;
        let options = TextInitOptions::new(self.model_name.clone())
            .with_cache_dir(self.cache_dir.clone())
            .with_show_download_progress(false)
            .with_intra_threads(2);
        self.embedder = Some(TextEmbedding::try_new(options)?);
        Ok(())
    }

    /// Drop the ONNX session and tokenizer when idle sleep begins.
    pub fn deactivate(&mut self) {
        self.embedder = None;
    }

    /// True only while the optional ONNX runtime is loaded.
    pub fn is_active(&self) -> bool {
        self.embedder.is_some()
    }

    /// Embed one bounded batch of new text chunks; call from the awake worker.
    pub fn index_unembedded_batch(&mut self, limit: usize) -> Result<usize, RetrievalError> {
        let Some(embedder) = self.embedder.as_mut() else {
            return Ok(0);
        };
        let batch = self.db.unembedded_chunks(limit.min(self.batch_size))?;
        if batch.is_empty() {
            return Ok(0);
        }
        let texts = batch
            .iter()
            .map(|row| row.text.as_str())
            .collect::<Vec<_>>();
        let embeddings = embedder.embed(texts, Some(self.batch_size))?;
        if embeddings.len() != batch.len() {
            return Err(RetrievalError::Model(
                "embedding model returned an unexpected batch length".into(),
            ));
        }
        let vectors = batch
            .into_iter()
            .zip(embeddings)
            .map(|(chunk, vector)| (chunk.chunk_id, vector))
            .collect::<Vec<_>>();
        let count = vectors.len();
        self.db.store_embeddings(&vectors)?;
        Ok(count)
    }

    /// Search with BM25-style FTS5 plus cosine similarity, fused by reciprocal ranks.
    /// If ONNX is unavailable or inactive, lexical search remains available offline.
    pub fn search(&mut self, query: &str, limit: usize) -> Result<Vec<SearchHit>, RetrievalError> {
        let query = self.db.expand_glossary(query.trim())?;
        let query = query.trim();
        if query.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        if query.chars().count() > 8000 {
            return Err(RetrievalError::Config(ConfigError::Invalid(
                "search query exceeds 8000 characters".into(),
            )));
        }
        let take = limit.saturating_mul(4).clamp(20, 1000);
        let lexical = self.db.search_lexical(query, take)?;
        if self.embedder.is_none() {
            return self.apply_feedback(lexical, limit.min(100));
        }

        // Indexing is incremental. This keeps each wake responsive and resumes on the
        // next search instead of embedding the entire corpus in one memory-heavy pass.
        self.index_unembedded_batch(self.batch_size)?;
        let embedder = self
            .embedder
            .as_mut()
            .expect("active embedding model is retained");
        let query_vector = embedder
            .embed([query], Some(1))?
            .into_iter()
            .next()
            .unwrap_or_default();
        let dense = self.db.search_dense(&query_vector, take)?;
        let fused = reciprocal_rank_fusion(dense, lexical, limit.min(100));
        self.apply_feedback(fused, limit.min(100))
    }

    fn apply_feedback(
        &self,
        mut hits: Vec<SearchHit>,
        limit: usize,
    ) -> Result<Vec<SearchHit>, RetrievalError> {
        for hit in &mut hits {
            hit.score += self.db.file_signal_boost(&hit.path)?;
        }
        hits.sort_by(|left, right| right.score.total_cmp(&left.score));
        hits.truncate(limit);
        Ok(hits)
    }

    /// The model label is exposed for diagnostics without exposing cache contents.
    pub fn model_label(&self) -> &str {
        &self.model_label
    }
}

fn resolve_model(label: &str) -> Result<EmbeddingModel, RetrievalError> {
    if label.eq_ignore_ascii_case("BAAI/bge-small-en-v1.5") {
        return Ok(EmbeddingModel::BGESmallENV15);
    }
    if let Ok(model) = EmbeddingModel::from_str(label) {
        return Ok(model);
    }
    fastembed::TextEmbedding::list_supported_models()
        .into_iter()
        .find(|info| info.model_code.eq_ignore_ascii_case(label))
        .map(|info| info.model)
        .ok_or_else(|| RetrievalError::Model(label.to_owned()))
}

/// Reciprocal Rank Fusion over dense cosine and SQLite FTS5 result lists.
pub fn reciprocal_rank_fusion(
    dense: Vec<SearchHit>,
    lexical: Vec<SearchHit>,
    limit: usize,
) -> Vec<SearchHit> {
    let mut fused: HashMap<String, (SearchHit, f64)> = HashMap::new();
    for ranking in [&dense, &lexical] {
        for (rank, hit) in ranking.iter().enumerate() {
            let entry = fused
                .entry(hit.chunk_id.clone())
                .or_insert_with(|| (hit.clone(), 0.0));
            entry.1 += 1.0 / (60.0 + rank as f64 + 1.0);
        }
    }
    let mut results = fused
        .into_values()
        .map(|(mut hit, score)| {
            hit.score = score;
            hit
        })
        .collect::<Vec<_>>();
    results.sort_by(|left, right| right.score.total_cmp(&left.score));
    results.truncate(limit);
    results
}

/// Measure model load duration without logging the filesystem or model cache path.
pub fn activate_with_timing(
    engine: &mut HybridEngine,
) -> Result<std::time::Duration, RetrievalError> {
    let start = Instant::now();
    engine.activate()?;
    Ok(start.elapsed())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::ChunkInput;
    use tempfile::tempdir;

    fn hit(id: &str, score: f64) -> SearchHit {
        SearchHit {
            chunk_id: id.into(),
            path: format!("C:\\docs\\{id}.txt"),
            file_name: format!("{id}.txt"),
            snippet: id.into(),
            page_or_line: None,
            heading: None,
            score,
        }
    }

    #[test]
    fn reciprocal_rank_fusion_rewards_overlapping_results_and_limits_output() {
        let dense = vec![hit("both", 0.95), hit("dense-only", 0.9)];
        let lexical = vec![hit("lexical-only", 2.0), hit("both", 1.0)];
        let results = reciprocal_rank_fusion(dense, lexical, 2);
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].chunk_id, "both");
    }

    #[test]
    fn engine_construction_stays_dormant_and_lexical_search_works_without_onnx() {
        let directory = tempdir().unwrap();
        let mut settings = crate::config::load_settings(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../config/settings.yaml"
        ))
        .unwrap();
        settings.models.embeddings.cache_dir = directory
            .path()
            .join("cache")
            .to_string_lossy()
            .into_owned();
        let db = Database::new(directory.path().join("db.sqlite3"));
        db.initialize().unwrap();
        db.replace_file(
            directory.path().join("note.md"),
            "hash",
            6,
            1.0,
            &[ChunkInput {
                text: "offline needle".into(),
                page_or_line: None,
                heading: None,
            }],
        )
        .unwrap();
        let mut engine = HybridEngine::new(&settings, db).unwrap();
        assert!(!engine.is_active());
        assert_eq!(engine.search("needle", 5).unwrap().len(), 1);
        assert!(!engine.is_active());
    }
}
