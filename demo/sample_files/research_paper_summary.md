# Paper summary: Retrieval-augmented generation
The paper shows that giving a language model relevant retrieved passages reduces made-up answers.
Key idea: combine keyword search with embeddings, then let the model answer from the retrieved text only.
Limitations: retrieval quality bounds answer quality; long documents need careful chunking.
